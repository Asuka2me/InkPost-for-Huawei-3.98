"""Local image source for InkSight /api/render firmware. Python 3 + Pillow."""
import io
import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from PIL import Image, ImageEnhance, ImageOps

ROOT = Path(__file__).resolve().parent
LOCK = threading.Lock()
CURRENT = ROOT / 'current.png'
LAST = {
    'last_seen': None, 'device_ip': None, 'sent': 0, 'total': 0,
    'phase': 'waiting' if CURRENT.exists() else 'empty',
}
TRANSFER = 0
PALETTE = [0, 0, 0, 255, 255, 255, 255, 220, 0, 220, 25, 25]

def atkinson(image, palette):
    width, height = image.size
    pixels = image.tobytes()
    indices = bytearray(width * height)
    colors = tuple(zip(PALETTE[::3], PALETTE[1::3], PALETTE[2::3]))
    # One padded pixel on the left and two on the right absorb edge errors.
    stride = (width + 3) * 3
    rows = [[0.0] * stride for _ in range(3)]
    for y in range(height):
        current, below, two_below = rows
        for x in range(width):
            index = y * width + x
            offset = index * 3
            slot = (x + 1) * 3
            red = pixels[offset] + current[slot]
            green = pixels[offset + 1] + current[slot + 1]
            blue = pixels[offset + 2] + current[slot + 2]
            best_distance = float('inf')
            for color_index, (r, g, b) in enumerate(colors):
                distance = (red - r)**2 + (green - g)**2 + (blue - b)**2
                if distance < best_distance:
                    best_distance, chosen = distance, color_index
            indices[index] = chosen
            r, g, b = colors[chosen]
            for channel, error in enumerate(((red - r)/8, (green - g)/8, (blue - b)/8)):
                i = slot + channel
                current[i + 3] += error
                current[i + 6] += error
                below[i - 3] += error
                below[i] += error
                below[i + 3] += error
                two_below[i] += error
        rows = [below, two_below, [0.0] * stride]
    result = Image.frombytes('P', image.size, bytes(indices))
    result.putpalette(palette.getpalette())
    return result

def convert_image(image, width, height, rotate=0, dither=True, fit='contain',
                  brightness=1, contrast=1, saturation=1, position=(0.5, 0.5),
                  algorithm=None):
    image = ImageOps.exif_transpose(image).convert('RGBA')
    background = Image.new('RGBA', image.size, 'white')
    background.alpha_composite(image)
    image = background.convert('RGB').rotate(rotate, expand=True)
    for enhancer, factor in ((ImageEnhance.Brightness, brightness),
                             (ImageEnhance.Contrast, contrast),
                             (ImageEnhance.Color, saturation)):
        if factor != 1:
            image = enhancer(image).enhance(factor)
    if fit == 'cover':
        image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS,
                             centering=position)
    else:
        image = ImageOps.pad(image, (width, height), Image.Resampling.LANCZOS,
                             color='white', centering=position)
    palette = Image.new('P', (1, 1))
    palette.putpalette(PALETTE + [0] * (768 - len(PALETTE)))
    if algorithm == 'atkinson':
        return atkinson(image, palette)
    if algorithm == 'mono':
        result = image.convert('L').point([0] * 128 + [1] * 128)
        result.putpalette(palette.getpalette())
        return result
    if algorithm == 'floyd':
        dither = True
    return image.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)

def pack(image):
    pixels = bytes(image.getdata())
    return bytes(((pixels[i] & 3) << 6) | ((pixels[i+1] & 3) << 4) |
                 ((pixels[i+2] & 3) << 2) | (pixels[i+3] & 3)
                 for i in range(0, len(pixels), 4))

PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>华为墨水屏传图</title><style>
body{font:17px system-ui;max-width:850px;margin:40px auto;padding:20px;background:#f5f3ef;color:#252525}
button,select,input{font:inherit;padding:9px;margin:8px 0}button{background:#245c4a;color:white;border:0;border-radius:6px;cursor:pointer}
img{max-width:100%;max-height:480px;background:white;border:1px solid #ccc}article{background:white;padding:22px;border-radius:12px;margin:20px 0}
</style><h1>华为四色墨水屏 · 本地传图</h1>
<p>适用于从服务器拉取图片的 InkSight 固件。USB 负责供电；图片通过 Wi-Fi 传输。</p>
<article><b>首次连接</b><p>电脑和屏幕连接同一个 Wi-Fi。在屏幕配网页面，将服务器地址改成本机局域网地址，端口为 <b>8765</b>。保存后重启屏幕。旧服务器地址请先记下，以便恢复。</p>
<p>例如 <code>http://10.230.241.145:8765</code>（这是开发时的地址；重连 Wi-Fi 后可能改变）。如页面分开填写端口，请按页面字段分别填写。电脑需保持开机，Windows 防火墙需允许此 Python 服务在当前网络接收连接。</p></article>
<article><input id="file" type="file" accept="image/*"><br>
<select id="size"><option value="768,552">横向 768 × 552</option><option value="552,768">竖向 552 × 768</option></select>
<select id="rotate"><option value="0">不旋转</option><option value="90">逆时针 90°</option><option value="180">180°</option><option value="270">顺时针 90°</option></select>
<select id="fit"><option value="contain">完整显示，留白</option><option value="cover">填满屏幕，居中裁切</option></select>
<label>渲染算法 <select id="algorithm">
<option value="atkinson">Atkinson（推荐，平滑）</option>
<option value="floyd" selected>Floyd–Steinberg（细节）</option>
<option value="mono">无抖动（仅黑白）</option>
</select></label><br>
<fieldset><legend>图像调整</legend>
<label>亮度 <input id="brightness" type="range" min="0" max="200" value="100"> <output id="brightnessValue">100%</output></label><br>
<label>对比度 <input id="contrast" type="range" min="0" max="200" value="100"> <output id="contrastValue">100%</output></label><br>
<label>饱和度 <input id="saturation" type="range" min="0" max="200" value="100"> <output id="saturationValue">100%</output></label><br>
<label>左右位置 <input id="positionX" type="range" min="0" max="100" value="50"> <output id="positionXValue">50%</output></label><br>
<label>上下位置 <input id="positionY" type="range" min="0" max="100" value="50"> <output id="positionYValue">50%</output></label><br>
<small>完整显示时，只有留白的方向可以移动。填满屏幕时，滑块调整保留的区域。</small><br>
<button type="button" id="resetAdjustments">恢复默认</button>
</fieldset>
<button onclick="generatePreview()" id="generate">生成预览</button>
<button onclick="publish()" id="send" disabled>发布到墨水屏</button>
<p id="result">选择图片并生成预览，满意后再发布。</p>
<img id="preview" hidden></article><article>
<b>墨水屏连接</b><p id="status" role="status"></p>
<b>图片传输</b><p id="transfer" role="status"></p>
<progress id="transferProgress" value="0" max="100" aria-label="墨水屏取图进度"></progress>
<p><small>墨水屏会主动取图。休眠时可能暂时没有请求，电脑无法确认它的 Wi-Fi 状态或实屏刷新进度。</small></p>
</article>
<script>
const get = id => document.getElementById(id);
let draft = null;
let previewUrl = null;
let revision = 0;
let previewTimer;
let displayedRevision = 0;
let rendering = false;
let previewQueued = false;
function clearPreview() {
  revision++;
  displayedRevision = revision;
  clearTimeout(previewTimer);
  previewTimer = undefined;
  previewQueued = false;
  draft = null;
  get('send').disabled = true;
  get('preview').hidden = true;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  get('result').textContent = '请生成预览。';
}
for (const id of ['file', 'size', 'rotate', 'fit']) {
  get(id).addEventListener('change', clearPreview);
}
get('algorithm').addEventListener('change', schedulePreview);
const adjustments = ['brightness', 'contrast', 'saturation', 'positionX', 'positionY'];
function schedulePreview() {
  revision++;
  draft = null;
  get('send').disabled = true;
  if (!get('file').files.length) return;
  get('result').textContent = '正在更新预览…';
  if (rendering) {
    previewQueued = true;
    return;
  }
  if (previewTimer === undefined) previewTimer = setTimeout(generatePreview, 90);
}
for (const id of adjustments) {
  get(id).addEventListener('input', () => {
    get(id + 'Value').value = get(id).value + '%';
    schedulePreview();
  });
}
get('resetAdjustments').addEventListener('click', () => {
  for (const id of adjustments) {
    get(id).value = get(id).defaultValue;
    get(id + 'Value').value = get(id).value + '%';
  }
  schedulePreview();
});
async function generatePreview() {
  clearTimeout(previewTimer);
  previewTimer = undefined;
  const file = get('file').files[0];
  if (!file) return alert('请先选择图片');
  if (rendering) {
    previewQueued = true;
    return;
  }
  rendering = true;
  const requestedRevision = ++revision;
  draft = null;
  get('send').disabled = true;
  get('result').textContent = '正在更新预览…';
  const [w, h] = get('size').value.split(',');
  const params = new URLSearchParams({w, h, rotate: get('rotate').value,
    fit: get('fit').value, algorithm: get('algorithm').value});
  for (const id of adjustments) params.set(id, get(id).value);
  try {
    const response = await fetch('/preview?' + params, {method: 'POST', body: file});
    if (!response.ok) throw Error(await response.text());
    const preview = await response.blob();
    if (requestedRevision < displayedRevision) return;
    displayedRevision = requestedRevision;
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = URL.createObjectURL(preview);
    get('preview').src = previewUrl;
    get('preview').hidden = false;
    if (revision !== requestedRevision) return;
    draft = preview;
    get('result').textContent = '预览已生成。满意后点「发布到墨水屏」。';
    get('send').disabled = false;
  } catch (error) {
    if (revision === requestedRevision) get('result').textContent = '预览失败：' + error.message;
  } finally {
    rendering = false;
    if (previewQueued) {
      previewQueued = false;
      schedulePreview();
    }
  }
}
async function publish() {
  if (!draft) return;
  const publishing = draft;
  get('send').disabled = true;
  try {
    const response = await fetch('/publish', {method: 'POST', body: publishing});
    if (!response.ok) throw Error(await response.text());
    if (draft === publishing) get('result').textContent = '已发布，等待墨水屏下次取图。';
  } catch (error) { alert(error.message); }
  finally { if (draft === publishing) get('send').disabled = false; }
}
async function status() {
  try {
    const response = await fetch('/status');
    if (!response.ok) throw Error();
    const state = await response.json();
    if (state.last_seen === null) {
      get('status').textContent = '连接状态未知 · 服务启动后尚未收到设备请求';
    } else {
      const recent = state.seconds_since_contact <= 60;
      const seen = new Date(state.last_seen * 1000).toLocaleTimeString();
      get('status').textContent = (recent ? '近期已联系' : '连接状态未知 · 可能休眠或断连')
        + ' · 最后联系 ' + seen + ' · ' + state.device_ip;
    }
    const percent = state.total ? Math.floor(state.sent * 100 / state.total) : 0;
    const phases = {
      empty: '请先发布一张图片',
      waiting: '等待墨水屏取图 · 0%',
      sending: '正在发送 · ' + Math.min(99, percent) + '%',
      sent: '图片数据已发送 · 100%',
      failed: '传输中断 · 等待墨水屏重试',
    };
    get('transfer').textContent = phases[state.phase];
    get('transferProgress').value = state.phase === 'sending' ? Math.min(99, percent) : percent;
  } catch {
    get('status').textContent = '本地传图服务未连接';
    get('transfer').textContent = '无法获取传输进度';
    get('transferProgress').value = 0;
  } finally {
    setTimeout(status, 500);
  }
}
status();
</script></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_request(self, code='-', size='-'):
        if self.path != '/status':
            super().log_request(code, size)

    def respond(self, data, content_type='application/json', code=200, transfer=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type + ('; charset=utf-8' if content_type.startswith('text/') else ''))
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if transfer is None:
            self.wfile.write(data)
            return
        for offset in range(0, len(data), 4096):
            chunk = data[offset:offset + 4096]
            self.wfile.write(chunk)
            with LOCK:
                if transfer == TRANSFER:
                    LAST['sent'] = offset + len(chunk)

    def record_contact(self, path):
        if path == '/api/render' or path.startswith('/api/device/'):
            with LOCK:
                LAST.update(last_seen=time.time(), device_ip=self.client_address[0])

    def do_GET(self):
        global TRANSFER
        parsed = urlparse(self.path)
        self.record_contact(parsed.path)
        q = parse_qs(parsed.query)
        if parsed.path == '/':
            return self.respond(PAGE, 'text/html')
        if parsed.path == '/status':
            with LOCK:
                status = dict(LAST)
            if status['last_seen'] is not None:
                status['seconds_since_contact'] = max(0, time.time() - status['last_seen'])
            return self.respond(status)
        if parsed.path.startswith('/api/device/') and parsed.path.endswith('/state'):
            with LOCK:
                pending = LAST['phase'] in ('waiting', 'failed')
            return self.respond({'pending_refresh': int(pending), 'pending_mode': ''})
        if parsed.path in ('/preview.png', '/api/render'):
            try:
                with LOCK:
                    with Image.open(CURRENT) as source:
                        image = source.copy()
                    if parsed.path == '/api/render':
                        TRANSFER += 1
                        transfer = TRANSFER
                if parsed.path == '/preview.png':
                    out = io.BytesIO()
                    image.save(out, 'PNG')
                    return self.respond(out.getvalue(), 'image/png')
                w, h = int(q.get('w', [768])[0]), int(q.get('h', [552])[0])
                if (w, h) not in ((768, 552), (552, 768)):
                    return self.respond({'error': 'Unsupported panel geometry'}, code=400)
                if image.size != (w, h):
                    image = convert_image(image, w, h)
                data = pack(image)
                with LOCK:
                    if transfer == TRANSFER:
                        LAST.update(phase='sending', sent=0, total=len(data))
                try:
                    self.respond(data, 'application/octet-stream', transfer=transfer)
                except OSError:
                    with LOCK:
                        if transfer == TRANSFER:
                            LAST['phase'] = 'failed'
                    return
                with LOCK:
                    if transfer == TRANSFER:
                        LAST['phase'] = 'sent'
                return
            except FileNotFoundError:
                return self.respond({'error': 'Upload an image first'}, code=503)
        return self.respond({'error': 'Not found'}, code=404)

    def do_POST(self):
        p = urlparse(self.path)
        self.record_contact(p.path)
        if p.path.startswith('/api/device/') and p.path.endswith('/heartbeat'):
            return self.respond({'ok': True})
        if p.path not in ('/upload', '/preview', '/publish'):
            return self.respond({'error': 'Not found'}, code=404)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 20 * 1024 * 1024:
                raise ValueError('图片大小应小于 20 MB')
            if p.path == '/publish':
                with Image.open(io.BytesIO(self.rfile.read(length))) as source:
                    if (source.format != 'PNG' or source.mode != 'P'
                            or source.size not in ((768, 552), (552, 768))
                            or source.getpalette()[:12] != PALETTE
                            or source.getextrema()[1] > 3):
                        raise ValueError('请先生成有效的四色预览')
                    image = source.copy()
                return self.publish_image(image)
            q = parse_qs(p.query)
            w, h = int(q.get('w', [768])[0]), int(q.get('h', [552])[0])
            if (w, h) not in ((768, 552), (552, 768)):
                raise ValueError('不支持的尺寸')
            adjustments = {}
            for name in ('brightness', 'contrast', 'saturation'):
                value = int(q.get(name, ['100'])[0])
                if not 0 <= value <= 200:
                    raise ValueError('图像调整范围为 0–200%')
                adjustments[name] = value / 100
            position = tuple(int(q.get(name, ['50'])[0]) / 100
                             for name in ('positionX', 'positionY'))
            if not all(0 <= value <= 1 for value in position):
                raise ValueError('图片位置范围为 0–100%')
            algorithm = q.get('algorithm', [None])[0]
            if algorithm not in (None, 'atkinson', 'floyd', 'mono'):
                raise ValueError('不支持的渲染算法')
            with Image.open(io.BytesIO(self.rfile.read(length))) as source:
                image = convert_image(source, w, h, int(q.get('rotate', [0])[0]),
                                      q.get('dither', ['1'])[0] == '1',
                                      q.get('fit', ['contain'])[0], position=position,
                                      algorithm=algorithm,
                                      **adjustments)
            if p.path == '/preview':
                out = io.BytesIO()
                image.save(out, 'PNG')
                return self.respond(out.getvalue(), 'image/png')
            return self.publish_image(image)
        except Exception as e:
            self.respond({'error': str(e)}, code=400)

    def publish_image(self, image):
        global TRANSFER
        with LOCK:
            pending = CURRENT.with_suffix('.tmp')
            image.save(pending, 'PNG')
            pending.replace(CURRENT)
            TRANSFER += 1
            LAST.update(phase='waiting', sent=0, total=0)
        self.respond({'ok': True})

if __name__ == '__main__':
    print('InkSight image server: http://127.0.0.1:8765', flush=True)
    print('Set device server to http://<PC LAN IP>:8765; Ctrl+C stops server.', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8765), Handler).serve_forever()
