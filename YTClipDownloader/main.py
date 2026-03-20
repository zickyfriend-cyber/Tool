import sys
import os
import re
import json
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import quote as _url_quote

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QPushButton, QLabel, QTextEdit, QTextBrowser, QSizePolicy, QFrame,
    QComboBox, QCheckBox, QProgressBar, QFileDialog,
    QSplitter, QTreeWidget, QTreeWidgetItem, QMenu, QMessageBox, QInputDialog,
    QTabWidget, QFileIconProvider, QDoubleSpinBox, QSpinBox,
)
from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEngineSettings
from PyQt5.QtCore import Qt, QTimer, QUrl, QProcess, pyqtSignal, QEvent, QMimeData, QFileInfo, QSettings
from PyQt5.QtGui import QFont, QPainter, QColor, QPen, QDrag, QBrush

# ---------------------------------------------------------------------------
# Local HTTP server  (YouTube requires http://localhost origin for embeds)
# ---------------------------------------------------------------------------

def _find_free_port():
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]

_SERVER_PORT = _find_free_port()
_HTML_BYTES: bytes = b''
_WIN_MUTEX   = None   # 다중 인스턴스 감지용 Windows 뮤텍스 핸들
_LOCAL_VIDEO_PATH: str = ''   # HTTP 서버에서 서빙할 로컬 영상 경로
_LOCAL_VIDEO_HTML: bytes = b''  # 해당 영상용 플레이어 HTML

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 쿼리스트링 제거 (캐시 버스팅용 ?t=N 무시)
        clean_path = self.path.split('?')[0]
        # /video  — 로컬 파일 HTTP 서빙 (Range 요청 지원)
        if clean_path in ('/video', '/video.mp4'):
            path = _LOCAL_VIDEO_PATH
            if not path or not os.path.isfile(path):
                self.send_response(404)
                self.end_headers()
                return
            size = os.path.getsize(path)
            # Range 헤더 파싱
            range_header = self.headers.get('Range', '')
            start, end = 0, size - 1
            if range_header.startswith('bytes='):
                parts = range_header[6:].split('-')
                try:
                    start = int(parts[0]) if parts[0] else 0
                    end   = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
                except ValueError:
                    pass
            length = end - start + 1
            status = 206 if range_header else 200
            self.send_response(status)
            ext = os.path.splitext(path)[1].lower()
            mime = {'mp4': 'video/mp4', 'webm': 'video/webm', 'mkv': 'video/x-matroska'}.get(ext[1:], 'video/mp4')
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if range_header:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            try:
                with open(path, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass  # 브라우저가 Range 요청 완료 후 연결 끊는 건 정상
            return
        # /local_player  — 로컬 영상 플레이어 HTML
        if clean_path == '/local_player':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_LOCAL_VIDEO_HTML)
            return
        # /  — YouTube 플레이어 HTML
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(_HTML_BYTES)
    def log_message(self, *a): pass

def _start_server():
    srv = HTTPServer(('127.0.0.1', _SERVER_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
APP_VERSION  = "1.2"
_GH_RAW      = "https://raw.githubusercontent.com/zickyfriend-cyber/Tool/main/YTClipDownloader"
_SCRIPT_URL  = f"{_GH_RAW}/main.py"
_GH_API_FILE = "https://api.github.com/repos/zickyfriend-cyber/Tool/contents/YTClipDownloader/main.py"
_SHA_CACHE   = None   # 런타임 캐시 (프로세스 재시작 전까지 유지)
# PyInstaller 번들 실행 시 sys.executable 기준, 일반 실행 시 __file__ 기준
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
YTDLP_DIR   = os.path.normpath(os.path.join(BASE_DIR, 'ytdlp'))
YTDLP_EXE   = os.path.join(YTDLP_DIR, 'yt-dlp.exe')
DOWNLOAD_DIR = os.path.join(BASE_DIR, 'download')
FFMPEG_DIR  = YTDLP_DIR
FFMPEG_EXE  = os.path.join(YTDLP_DIR, 'ffmpeg.exe')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def secs_to_hms(s: float) -> str:
    s = max(0, int(s))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def secs_to_duration(s: float) -> str:
    """재생 길이를 간결하게 표시 (예: 5초, 1:23, 1:02:03)."""
    s = max(0, int(s))
    if s < 60:
        return f"{s}초"
    elif s < 3600:
        return f"{s//60}:{s%60:02d}"
    else:
        return f"{s//3600}:{(s%3600)//60:02d}:{s%60:02d}"

def hms_to_secs(t: str) -> int:
    parts = t.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
        if len(parts) == 2:
            return int(parts[0])*60 + int(parts[1])
        return int(parts[0])
    except Exception:
        return 0

def is_youtube(url: str) -> bool:
    return bool(re.search(r'(youtube\.com|youtu\.be)', url, re.I))

def extract_yt_id(url: str):
    for pat in [r'[?&]v=([a-zA-Z0-9_-]{11})',
                r'youtu\.be/([a-zA-Z0-9_-]{11})',
                r'embed/([a-zA-Z0-9_-]{11})',
                r'^([a-zA-Z0-9_-]{11})$']:
        m = re.search(pat, url.strip())
        if m:
            return m.group(1)
    return None

# ---------------------------------------------------------------------------
# Format options
# ---------------------------------------------------------------------------
RES_OPTIONS = ['최고화질', '1080p', '720p', '480p', '360p']
EXT_OPTIONS     = ['mp4', 'mkv', 'webm', 'mp3 (오디오만)', 'gif']
GIF_FPS_OPTIONS = ['8', '10', '12', '15', '20', '24', '30']
GIF_W_OPTIONS   = ['320', '480', '640', '960', '1280', '원본']

RES_FORMAT = {
    '최고화질': 'bv*+ba/b',
    '1080p':   'bv[height<=1080]+ba/b[height<=1080]',
    '720p':    'bv[height<=720]+ba/b[height<=720]',
    '480p':    'bv[height<=480]+ba/b[height<=480]',
    '360p':    'bv[height<=360]+ba/b[height<=360]',
}
# MP4 전용: m4a(AAC) 오디오 스트림 우선 선택 → Opus-in-MP4 방지
RES_FORMAT_MP4 = {
    '최고화질': 'bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b',
    '1080p':   'bv[height<=1080][ext=mp4]+ba[ext=m4a]/bv[height<=1080]+ba/b[height<=1080]',
    '720p':    'bv[height<=720][ext=mp4]+ba[ext=m4a]/bv[height<=720]+ba/b[height<=720]',
    '480p':    'bv[height<=480][ext=mp4]+ba[ext=m4a]/bv[height<=480]+ba/b[height<=480]',
    '360p':    'bv[height<=360][ext=mp4]+ba[ext=m4a]/bv[height<=360]+ba/b[height<=360]',
}
# 소리 제거 모드: 비디오 스트림만 선택
RES_FORMAT_MUTE = {
    '최고화질': 'bv*/b',
    '1080p':   'bv[height<=1080]/b[height<=1080]',
    '720p':    'bv[height<=720]/b[height<=720]',
    '480p':    'bv[height<=480]/b[height<=480]',
    '360p':    'bv[height<=360]/b[height<=360]',
}

def unique_path(path: str) -> str:
    """파일이 이미 존재하면 'name (1).ext', 'name (2).ext' 형태로 반환."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _build_atempo(speed: float) -> str:
    """atempo 필터 체인 생성 — 범위 0.5~2.0 벗어나면 체인으로 연결."""
    filters = []
    s = speed
    while s > 2.0:
        filters.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        filters.append("atempo=0.5")
        s *= 2.0
    filters.append(f"atempo={s:.4f}")
    return ','.join(filters)


def build_format_args(res: str, ext: str, mute: bool = False):
    """Returns (yt-dlp format args list, actual file extension string)"""
    if ext == 'mp3 (오디오만)':
        return ['-f', 'ba/b', '-x', '--audio-format', 'mp3'], 'mp3'
    if mute:
        fmt = RES_FORMAT_MUTE.get(res, 'bv*/b')
    elif ext == 'mp4':
        # MP4는 m4a(AAC) 오디오 스트림 우선 선택 → Opus-in-MP4 방지
        fmt = RES_FORMAT_MP4.get(res, 'bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b')
    else:
        fmt = RES_FORMAT.get(res, 'bv*+ba/b')
    args = ['-f', fmt, '--merge-output-format', ext]
    return args, ext

# ---------------------------------------------------------------------------
# HTML template for YouTube IFrame player
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:100%; height:100%; background:#111; overflow:hidden; }
#player { width:100%; height:100%; }
#placeholder { display:flex; align-items:center; justify-content:center;
  height:100%; color:#555; font-family:sans-serif; font-size:15px; }
</style>
</head>
<body>
<div id="player"><div id="placeholder">URL을 입력하고 로드 버튼을 누르세요</div></div>
<script>
var player=null, ytReady=false, pendingId=null;
function onYouTubeIframeAPIReady(){
  ytReady=true;
  if(pendingId){_create(pendingId);pendingId=null;}
}
var _ytError=null;
function _setHQ(p){
  try{p.setPlaybackQuality('hd1080');}catch(e){}
}
function _create(id){
  player=new YT.Player('player',{
    videoId:id, width:'100%', height:'100%',
    playerVars:{playsinline:1, rel:0, vq:'hd1080'},
    events:{
      onReady:function(e){e.target.playVideo();_setHQ(e.target);},
      onStateChange:function(e){if(e.data===1)_setHQ(e.target);},
      onError:function(e){_ytError=e.data;}
    }
  });
}
function getYTError(){return _ytError;}
function clearYTError(){_ytError=null;}
function loadVideo(id){
  if(!ytReady){pendingId=id;return;}
  if(player&&player.loadVideoById){
    player.loadVideoById({videoId:id,suggestedQuality:'hd1080'});
    _setHQ(player);
  }else{_create(id);}
}
function getCurrentTime()   { return (player&&player.getCurrentTime)?player.getCurrentTime():0; }
function getDuration()      { return (player&&player.getDuration)?player.getDuration():0; }
function seekTo(s)          { if(player&&player.seekTo) player.seekTo(s,true); }
function getVideoTitle()    { return (player&&player.getVideoData)?player.getVideoData().title||'':''; }
function setRate(r)         { if(player&&player.setPlaybackRate) player.setPlaybackRate(r); }
function getPlaybackRate()  { return (player&&player.getPlaybackRate)?player.getPlaybackRate():1; }
function togglePlay()       { if(!player) return; var s=player.getPlayerState(); if(s===1)player.pauseVideo(); else player.playVideo(); }
document.addEventListener('click', function(e) { if(e.target.tagName!=='IFRAME') togglePlay(); });
(function(){
  var s=document.createElement('script');
  s.src='https://www.youtube.com/iframe_api';
  document.head.appendChild(s);
})();
</script>
</body></html>
"""

PLACEHOLDER_HTML = """<!DOCTYPE html><html><body style="
  background:#111;color:#888;display:flex;align-items:center;
  justify-content:center;height:100%;margin:0;font-family:sans-serif">
  <div style="text-align:center">
    <div style="font-size:32px;margin-bottom:10px">⬇</div>
    <div>미리보기를 지원하지 않는 URL입니다.</div>
    <div style="margin-top:6px;color:#555;font-size:12px">
      YouTube 외 사이트는 다운로드만 가능합니다.</div>
  </div>
</body></html>"""

# ---------------------------------------------------------------------------
# Range Slider  (dual-handle)
# ---------------------------------------------------------------------------
class RangeSlider(QWidget):
    startChanged  = pyqtSignal(int)
    endChanged    = pyqtSignal(int)
    startReleased = pyqtSignal()
    endReleased   = pyqtSignal()

    HW = 6    # handle half-width  (total: 12px)
    HH = 12   # handle half-height (total: 24px)
    TH = 7    # track height px
    R  = HW   # alias used in geometry (keep compat)
    CY = 32   # fixed track center y (위 구간길이 + 아래 시간 레이블 공간 확보)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64)
        self.setMouseTracking(True)
        self._min   = 0
        self._max   = 600
        self._start = 0
        self._end   = 600
        self._drag  = None  # 'start' | 'end'

    # ---- public ----
    def setRange(self, mn: int, mx: int):
        self._min = mn
        self._max = max(mn + 1, mx)
        # _start는 max-1 이하로 클램프 → start==end==max 잠김 버그 방지
        self._start = max(mn, min(self._start, self._max - 1))
        self._end   = max(self._start + 1, min(self._end, self._max))
        self.update()

    def setStart(self, v: int, silent=True):
        v = max(self._min, min(int(v), self._end))
        if v != self._start:
            self._start = v
            if not silent:
                self.startChanged.emit(v)
            self.update()

    def setEnd(self, v: int, silent=True):
        v = max(self._start, min(int(v), self._max))
        if v != self._end:
            self._end = v
            if not silent:
                self.endChanged.emit(v)
            self.update()

    @property
    def start(self)   -> int: return self._start
    @property
    def end(self)     -> int: return self._end
    @property
    def minimum(self) -> int: return self._min
    @property
    def maximum(self) -> int: return self._max

    # ---- geometry ----
    def _to_x(self, val: int) -> float:
        ratio = (val - self._min) / max(1, self._max - self._min)
        return self.HW + ratio * (self.width() - 2 * self.HW)

    def _to_val(self, x: int) -> float:
        ratio = max(0.0, min(1.0, (x - self.HW) / max(1, self.width() - 2 * self.HW)))
        return self._min + ratio * (self._max - self._min)

    def _draw_positions(self):
        xs = self._to_x(self._start)
        xe = self._to_x(self._end)
        if abs(xs - xe) < 2 * self.HW:
            mid = (xs + xe) / 2
            return xs, xe, mid - self.HW, mid + self.HW
        return xs, xe, xs, xe

    def _hit(self, x: int):
        _, _, xs_d, xe_d = self._draw_positions()
        hw = self.HW + 4
        ds = abs(x - xs_d)
        de = abs(x - xe_d)
        if ds <= hw and ds <= de: return 'start'
        if de <= hw:              return 'end'
        return None

    # ---- paint ----
    def paintEvent(self, _ev):
        p  = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cy = self.CY
        hw, hh, th = self.HW, self.HH, self.TH

        xs_l, xe_l, xs_d, xe_d = self._draw_positions()

        # background track
        xl = self._to_x(self._min);  xr = self._to_x(self._max)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#444'))
        p.drawRoundedRect(int(xl), cy - th//2, int(xr - xl), th, 3, 3)

        # selected range
        p.setBrush(QColor('#3a8ee6'))
        p.drawRoundedRect(int(xs_l), cy - th//2, max(0, int(xe_l - xs_l)), th, 3, 3)

        # start handle — 파란 직사각형
        p.setBrush(QColor('#2196F3'))
        p.setPen(QPen(QColor('#fff'), 1.5))
        p.drawRoundedRect(int(xs_d - hw), cy - hh, 2*hw, 2*hh, 3, 3)

        # end handle — 빨간 직사각형
        p.setBrush(QColor('#F44336'))
        p.setPen(QPen(QColor('#fff'), 1.5))
        p.drawRoundedRect(int(xe_d - hw), cy - hh, 2*hw, 2*hh, 3, 3)

        # 드래그 중인 핸들 아래 시간 레이블 + 위에 구간 길이 (드래그 중인 것만 표시)
        if self._drag is not None:
            font = p.font()
            font.setPointSize(8)
            p.setFont(font)
            fm = p.fontMetrics()
            if self._drag == 'start':
                val, xd, color = self._start, xs_d, '#5bb8ff'
            else:
                val, xd, color = self._end, xe_d, '#ff7a70'

            # 핸들 아래: 현재 시간
            text = secs_to_hms(val)
            tw = fm.horizontalAdvance(text)
            tx = int(xd - tw / 2)
            tx = max(0, min(tx, self.width() - tw))
            p.setPen(QColor(color))
            p.drawText(tx, cy + hh + 3 + fm.ascent(), text)

            # 핸들 위: 구간 길이
            dur_text = secs_to_duration(self._end - self._start)
            dw = fm.horizontalAdvance(dur_text)
            dx = int(xd - dw / 2)
            dx = max(0, min(dx, self.width() - dw))
            p.setPen(QColor('#aaa'))
            p.drawText(dx, cy - hh - 3, dur_text)

        p.end()

    # ---- mouse ----
    def mousePressEvent(self, ev):
        self._drag = self._hit(ev.x())
        if self._drag:
            self.update()

    def mouseMoveEvent(self, ev):
        if self._drag:
            val = int(self._to_val(ev.x()))
            if self._drag == 'start':
                new = max(self._min, min(val, self._end - 1))
                self._start = new
                self.startChanged.emit(new)
                self.update()
            else:
                new = max(self._start + 1, min(val, self._max))
                self._end = new
                self.endChanged.emit(new)
                self.update()
        else:
            self.setCursor(Qt.SizeHorCursor if self._hit(ev.x()) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev):
        if self._drag == 'start': self.startReleased.emit()
        elif self._drag == 'end': self.endReleased.emit()
        self._drag = None
        self.update()

# ---------------------------------------------------------------------------
# FileTree  — 계층 파일 트리 (드래그·F2 지원)
# ---------------------------------------------------------------------------
class FileTree(QTreeWidget):
    f2Pressed  = pyqtSignal()
    fileMoved     = pyqtSignal(str, str)   # src_path, dst_path
    moveError     = pyqtSignal(str)
    deletePressed = pyqtSignal()
    enterPressed  = pyqtSignal()

    def keyPressEvent(self, ev):
        k = ev.key()
        if k == Qt.Key_F2:
            self.f2Pressed.emit()
        elif k == Qt.Key_Delete:
            self.deletePressed.emit()
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            self.enterPressed.emit()
        else:
            super().keyPressEvent(ev)

    def startDrag(self, actions):
        item = self.currentItem()
        if not item:
            return
        kind = item.data(0, Qt.UserRole + 1)
        path = item.data(0, Qt.UserRole)
        if not path or kind not in ('file', 'dir'):
            return
        drag = QDrag(self)
        mime = QMimeData()
        # 파일이면 URL도 포함 (WebView 드롭용)
        if kind == 'file' and os.path.isfile(path):
            mime.setUrls([QUrl.fromLocalFile(path)])
        # 내부 이동용 경로 텍스트
        mime.setText(path)
        drag.setMimeData(mime)
        drag.exec_(Qt.MoveAction | Qt.CopyAction)

    def dragEnterEvent(self, ev):
        if ev.source() is self:
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.source() is self:
            target = self.itemAt(ev.pos())
            if target and target.data(0, Qt.UserRole + 1) == 'dir':
                ev.acceptProposedAction()
            else:
                ev.ignore()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if ev.source() is not self:
            super().dropEvent(ev)
            return
        target = self.itemAt(ev.pos())
        if not target or target.data(0, Qt.UserRole + 1) != 'dir':
            ev.ignore()
            return
        item = self.currentItem()
        if not item or item.data(0, Qt.UserRole + 1) not in ('file', 'dir'):
            ev.ignore()
            return
        src_path = item.data(0, Qt.UserRole)
        dst_dir  = target.data(0, Qt.UserRole)
        dst_path = os.path.join(dst_dir, os.path.basename(src_path))
        # 같은 위치로 드롭하거나 자기 자신 안으로 이동 방지
        if src_path == dst_path or dst_dir.startswith(src_path + os.sep):
            ev.ignore()
            return
        try:
            import shutil
            shutil.move(src_path, dst_path)
            src_parent = item.parent() or self.invisibleRootItem()
            src_parent.removeChild(item)
            item.setData(0, Qt.UserRole, dst_path)
            target.addChild(item)
            self.expandItem(target)
            self.setCurrentItem(item)
            self.fileMoved.emit(src_path, dst_path)
        except Exception as e:
            self.moveError.emit(str(e))
        ev.acceptProposedAction()


# ---------------------------------------------------------------------------
# _OverlayLabel  — 미리보기 창 클릭 감지용 오버레이
# ---------------------------------------------------------------------------
class _OverlayLabel(QLabel):
    clicked = pyqtSignal()
    fileDropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            "background: rgba(0,0,0,160); color: #fff; font-size: 15px; border-radius: 6px;")
        self.setAcceptDrops(True)
        self.hide()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
        ev.accept()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        if ev.mimeData().hasUrls():
            for url in ev.mimeData().urls():
                path = url.toLocalFile()
                if path:
                    self.fileDropped.emit(path)
                    break
            ev.acceptProposedAction()


# ---------------------------------------------------------------------------
# _AspectContainer  — 16:9 비율을 유지하며 내부 위젯을 배치하는 컨테이너
# ---------------------------------------------------------------------------
class _AspectContainer(QWidget):
    overlayClicked = pyqtSignal()

    def __init__(self, child: 'QWidget', parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding))
        self.setMinimumSize(320, 180)
        self.setStyleSheet("background:#000;")
        self._child = child
        child.setParent(self)
        self._overlay = _OverlayLabel(self)
        self._overlay.clicked.connect(self.overlayClicked)
        self._overlay.fileDropped.connect(self._child._handle_drop_path)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        if ev.mimeData().hasUrls():
            self._child._handle_drop_mime(ev.mimeData())
            ev.acceptProposedAction()

    def show_overlay(self, text: str):
        self._overlay.setText(text)
        self._overlay.show()
        self._overlay.raise_()

    def hide_overlay(self):
        self._overlay.hide()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        w, h = self.width(), self.height()
        th = w * 9 // 16          # 폭 기준 높이
        tw = h * 16 // 9          # 높이 기준 폭
        if th <= h:               # 폭이 제한 요소
            cw, ch = w, th
        else:                     # 높이가 제한 요소
            cw, ch = tw, h
        cx, cy = (w - cw) // 2, (h - ch) // 2
        self._child.setGeometry(cx, cy, cw, ch)
        self._overlay.setGeometry(cx, cy, cw, ch)


# ---------------------------------------------------------------------------
# DroppableWebView  — 파일 드래그 수신 가능한 WebEngineView
# ---------------------------------------------------------------------------
class DroppableWebView(QWebEngineView):
    fileDropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def childEvent(self, ev):
        super().childEvent(ev)
        if ev.type() == QEvent.ChildAdded:
            child = ev.child()
            if hasattr(child, 'setAcceptDrops') and hasattr(child, 'installEventFilter'):
                child.setAcceptDrops(True)
                child.installEventFilter(self)

    def _handle_drop_path(self, path: str):
        if path and os.path.isfile(path):
            self.fileDropped.emit(path)

    def _handle_drop_mime(self, mime):
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path and os.path.isfile(path):
                    self.fileDropped.emit(path)
                    return True
        return False

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragEnterEvent(ev)

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
        else:
            super().dragMoveEvent(ev)

    def dropEvent(self, ev):
        if self._handle_drop_mime(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            super().dropEvent(ev)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.DragEnter and ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return True
        if t == QEvent.DragMove and ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            return True
        if t == QEvent.Drop and self._handle_drop_mime(ev.mimeData()):
            ev.acceptProposedAction()
            return True
        return super().eventFilter(obj, ev)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Clip Downloader v{APP_VERSION}  (YouTube / Instagram / TikTok / 외)")
        self.resize(1200, 960)
        # 이전 세션에서 남은 프록시 임시 파일 정리
        for f in os.listdir(BASE_DIR):
            if f.startswith('_preview_proxy_') and (f.endswith('.webm') or f.endswith('.mp4')):
                try:
                    os.remove(os.path.join(BASE_DIR, f))
                except Exception:
                    pass
        self._duration      = 600
        self._yt_loaded     = False
        self._upd_start     = False
        self._upd_end       = False
        self._process       = None
        self._pending_yt_id = None
        self._mode          = 'url'    # 'url' | 'local'
        self._local_file    = None
        self._local_is_gif  = False
        self._clip_secs     = 1
        self._rename_old_path = None
        self._icon_provider   = QFileIconProvider()
        self._speed           = 1.0
        self._speed_temp      = None
        self._sort_col        = 0
        self._sort_asc        = True
        self._new_file_paths: set = set()
        self._last_saved_path = None
        self._gif_proxy_mode = False  # GIF→MP4 프록시 변환 완료 후 autoplay 제어용
        self._proxy_counter  = 0      # 프록시 파일 고유 번호 (캐시 충돌 방지)
        # 오디오 유무 캐시 (path -> True/False)
        self._audio_cache: dict = {}
        self._audio_pending: list = []   # 확인 대기 중인 파일 경로
        self._audio_check_proc = None    # 현재 실행 중인 ffprobe QProcess
        self._path_to_tree_item: dict = {}  # path -> QTreeWidgetItem
        self._dl_start_time   = 0.0
        self._url_info_proc   = None   # yt-dlp -j 미리보기 정보 프로세스
        self._closing         = False  # closeEvent 진입 여부
        self._cookie_file     = None   # 쿠키 파일 경로
        # 영상 큐
        self._queue: list = []
        self._queue_running   = False
        self._queue_idx       = 0
        self._queue_results: list = []
        self._queue_result_durations: dict = {}  # path → clip duration(s)
        # GIF 큐
        self._gif_queue: list = []
        self._gif_queue_running  = False
        self._gif_queue_idx      = 0
        self._gif_queue_results: list = []
        self._pending_segment   = None   # (start_s, end_s) — 큐 더블클릭 후 슬라이더 복원용
        self._pending_gif_seek  = False  # GIF 프록시 로드 완료 후 start 구간으로 seek

        self._build_ui()
        self._setup_timers()
        self._fetch_versions()
        self._restore_settings()
        # 시작 시 백그라운드 업데이트 확인 (3초 후, UI 로드 완료 후)
        QTimer.singleShot(3000, self._check_update_background)
        _ver = QLabel(f"  v{APP_VERSION}  ")
        _ver.setStyleSheet("color:#7ec8e3; font-size:11px;")
        self.statusBar().addPermanentWidget(_ver)
        _credit = QLabel("  Made by aram  ")
        _credit.setStyleSheet("color:#666; font-size:11px;")
        self.statusBar().addPermanentWidget(_credit)

    # -----------------------------------------------------------------------
    # UI construction
    # -----------------------------------------------------------------------
    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)
        self._splitter = splitter
        self.setCentralWidget(splitter)

        # ── 왼쪽 패널: 수직 QSplitter ─────────────────────────────────────────
        left = QWidget()
        left_outer = QVBoxLayout(left)
        left_outer.setSpacing(0)
        left_outer.setContentsMargins(0, 0, 0, 0)
        splitter.addWidget(left)

        self._left_splitter = QSplitter(Qt.Vertical)
        self._left_splitter.setHandleWidth(6)
        self._left_splitter.setStyleSheet(
            "QSplitter::handle:vertical {"
            "  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "    stop:0 #555, stop:0.4 #888, stop:0.6 #888, stop:1 #555);"
            "  border-radius: 3px; margin: 0 40px;"
            "}"
            "QSplitter::handle:vertical:hover {"
            "  background: qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            "    stop:0 #777, stop:0.4 #aaa, stop:0.6 #aaa, stop:1 #777);"
            "}"
        )
        left_outer.addWidget(self._left_splitter)

        # ── 상단 위젯 ─────────────────────────────────────────────────────────
        _top_w = QWidget()
        top_lay = QVBoxLayout(_top_w)
        top_lay.setSpacing(8)
        top_lay.setContentsMargins(12, 12, 12, 4)
        self._left_splitter.addWidget(_top_w)

        # ── 소스 선택 (토글 버튼) ─────────────────────────────────────────────
        src_row = QHBoxLayout()
        self._src_url_btn   = QPushButton("🌐 URL")
        self._src_local_btn = QPushButton("📁 로컬 파일")
        for btn in (self._src_url_btn, self._src_local_btn):
            btn.setCheckable(True)
            btn.setFixedHeight(26)
            btn.setStyleSheet(
                "QPushButton{border:1px solid #555;border-radius:4px;padding:0 10px;background:#2b2b2b;color:#ccc;}"
                "QPushButton:checked{background:#0066cc;color:#fff;border-color:#0055aa;}"
                "QPushButton:hover:!checked{background:#3a3a3a;}"
            )
        self._src_url_btn.setChecked(True)
        self._src_url_btn.clicked.connect(lambda: self._set_source(0))
        self._src_local_btn.clicked.connect(lambda: self._set_source(1))
        src_row.addWidget(self._src_url_btn)
        src_row.addWidget(self._src_local_btn)
        src_row.addStretch()
        self._pin_btn = QPushButton("📌 항상 위")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedSize(90, 26)
        self._pin_btn.setToolTip("다른 창 위에 항상 표시")
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        src_row.addWidget(self._pin_btn)
        top_lay.addLayout(src_row)

        # ── URL row ──────────────────────────────────────────────────────────
        self.url_widget = QWidget()
        url_lay = QHBoxLayout(self.url_widget)
        url_lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("URL:"); lbl.setFixedWidth(32)
        self.url_input = QComboBox()
        self.url_input.setEditable(True)
        self.url_input.setInsertPolicy(QComboBox.NoInsert)
        self.url_input.lineEdit().setPlaceholderText(
            "YouTube, Facebook 등 URL 입력  (예: https://www.youtube.com/watch?v=...)")
        self.url_input.lineEdit().returnPressed.connect(self.load_video)
        self.url_input.lineEdit().textChanged.connect(self._update_overlay)
        btn_load = QPushButton("로드"); btn_load.setFixedWidth(64)
        btn_load.clicked.connect(self.load_video)
        url_lay.addWidget(lbl); url_lay.addWidget(self.url_input); url_lay.addWidget(btn_load)
        top_lay.addWidget(self.url_widget)

        # ── Local file row ────────────────────────────────────────────────────
        self.local_widget = QWidget()
        local_lay = QHBoxLayout(self.local_widget)
        local_lay.setContentsMargins(0, 0, 0, 0)
        local_lay.addWidget(QLabel("파일:"))
        self.local_path_lbl = QLineEdit()
        self.local_path_lbl.setPlaceholderText("파일이 선택되지 않았습니다.")
        self.local_path_lbl.setReadOnly(True)
        local_lay.addWidget(self.local_path_lbl)
        btn_file = QPushButton("파일 선택"); btn_file.setFixedWidth(80)
        btn_file.clicked.connect(self._browse_local_file)
        local_lay.addWidget(btn_file)
        self.local_widget.setVisible(False)
        top_lay.addWidget(self.local_widget)

        # ── Web player ───────────────────────────────────────────────────────
        self.web = DroppableWebView()
        self.web.settings().setAttribute(QWebEngineSettings.JavascriptEnabled, True)
        self.web.settings().setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        self.web.setUrl(QUrl(f"http://127.0.0.1:{_SERVER_PORT}/"))
        self.web.loadFinished.connect(self._on_page_loaded)
        self.web.fileDropped.connect(self._on_file_dropped_to_player)
        self._web_container = _AspectContainer(self.web)
        self._web_container.overlayClicked.connect(self._on_overlay_clicked)
        top_lay.addWidget(self._web_container)

        # ── 하단 위젯 ─────────────────────────────────────────────────────────
        _bot_w = QWidget()
        bot_lay = QVBoxLayout(_bot_w)
        bot_lay.setSpacing(8)
        bot_lay.setContentsMargins(12, 12, 12, 12)
        self._left_splitter.addWidget(_bot_w)

        # ── Current time + speed + loop ──────────────────────────────────────
        row2 = QHBoxLayout()
        self.cur_lbl = QLabel("현재 위치:  00:00:00")
        f10 = QFont(); f10.setPointSize(10)
        self.cur_lbl.setFont(f10)
        row2.addWidget(self.cur_lbl)
        row2.addStretch()
        row2.addWidget(QLabel("재생 속도:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(['0.25x', '0.5x', '0.75x', '1x', '1.25x', '1.5x', '2x', '3x'])
        self.speed_combo.setCurrentText('1x')
        self.speed_combo.setFixedWidth(72)
        self.speed_combo.setToolTip("YouTube 내장 속도 조절과 동일한 API를 사용합니다.\n저장 시에도 이 속도가 적용됩니다.")
        self.speed_combo.currentTextChanged.connect(self._on_speed_changed)
        row2.addWidget(self.speed_combo)
        row2.addSpacing(12)
        self.loop_chk = QCheckBox("구간 반복 재생")
        row2.addWidget(self.loop_chk)
        bot_lay.addLayout(row2)

        # ── Range slider ─────────────────────────────────────────────────────
        self.rslider = RangeSlider()
        self.rslider.startChanged.connect(self._on_slider_start)
        self.rslider.endChanged.connect(self._on_slider_end)
        self.rslider.startReleased.connect(self._seek_start)
        self.rslider.endReleased.connect(self._seek_end)
        bot_lay.addWidget(self.rslider)

        # ── Time inputs ──────────────────────────────────────────────────────
        H = 32   # 버튼/입력창 공통 높이
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("시작:"))
        self.start_in = QLineEdit("00:00:00")
        self.start_in.setFixedSize(100, H)
        self.start_in.textEdited.connect(self._on_start_text)
        row3.addWidget(self.start_in)
        b = QPushButton("📍시작"); b.setFixedSize(72, H); b.clicked.connect(self._set_start_from_cur)
        b.setToolTip("현재 재생 위치를 시작 시간으로 지정")
        row3.addWidget(b)
        b = QPushButton("↵ 이동"); b.setFixedSize(66, H); b.clicked.connect(self._seek_start)
        b.setToolTip("입력한 시작 시간으로 이동")
        row3.addWidget(b)

        row3.addSpacing(16)

        row3.addWidget(QLabel("종료:"))
        self.end_in = QLineEdit("00:00:10")
        self.end_in.setFixedSize(100, H)
        self.end_in.textEdited.connect(self._on_end_text)
        row3.addWidget(self.end_in)
        b = QPushButton("📍종료"); b.setFixedSize(72, H); b.clicked.connect(self._set_end_from_cur)
        b.setToolTip("현재 재생 위치를 종료 시간으로 지정")
        row3.addWidget(b)
        b = QPushButton("↵ 이동"); b.setFixedSize(66, H); b.clicked.connect(self._seek_end_confirm)
        b.setToolTip("입력한 종료 시간으로 이동")
        row3.addWidget(b)

        row3.addStretch()
        self.dur_lbl = QLabel("구간: 00:00:10")
        row3.addWidget(self.dur_lbl)
        self.size_est_lbl = QLabel("")
        f_est = QFont(); f_est.setPointSize(9)
        self.size_est_lbl.setFont(f_est)
        self.size_est_lbl.setStyleSheet("color: #888;")
        row3.addWidget(self.size_est_lbl)
        bot_lay.addLayout(row3)

        bot_lay.addWidget(self._hline())

        # ── Format options ───────────────────────────────────────────────────
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("해상도:"))
        self.res_combo = QComboBox(); self.res_combo.addItems(RES_OPTIONS)
        self.res_combo.setFixedSize(120, H)
        fmt_row.addWidget(self.res_combo)

        fmt_row.addSpacing(16)
        fmt_row.addWidget(QLabel("저장 형식:"))
        self.ext_combo = QComboBox(); self.ext_combo.addItems(EXT_OPTIONS)
        self.ext_combo.setFixedSize(150, H)
        self.ext_combo.currentTextChanged.connect(self._on_ext_changed)
        self.res_combo.currentTextChanged.connect(self._update_size_estimate)
        fmt_row.addWidget(self.ext_combo)

        fmt_row.addSpacing(16)
        fmt_row.addWidget(QLabel("저장 확장자:"))
        self.ext_lbl = QLabel(".mp4")
        fb = QFont(); fb.setBold(True)
        self.ext_lbl.setFont(fb)
        fmt_row.addWidget(self.ext_lbl)
        fmt_row.addSpacing(16)
        self.mute_chk = QCheckBox("소리 제거")
        self.mute_chk.setToolTip("출력 파일에서 오디오를 제거합니다.")
        self.mute_chk.stateChanged.connect(self._update_size_estimate)
        fmt_row.addWidget(self.mute_chk)
        fmt_row.addStretch()
        bot_lay.addLayout(fmt_row)

        # ── GIF options (gif 선택 시에만 표시) ────────────────────────────────
        self.gif_row = QWidget()
        gif_lay = QHBoxLayout(self.gif_row)
        gif_lay.setContentsMargins(0, 0, 0, 0)
        gif_lay.addWidget(QLabel("GIF FPS:"))
        self.gif_fps = QComboBox(); self.gif_fps.addItems(GIF_FPS_OPTIONS)
        self.gif_fps.setCurrentText('10'); self.gif_fps.setFixedWidth(60)
        gif_lay.addWidget(self.gif_fps)
        gif_lay.addSpacing(12)
        gif_lay.addWidget(QLabel("폭(px):"))
        self.gif_width = QComboBox(); self.gif_width.addItems(GIF_W_OPTIONS)
        self.gif_width.setCurrentText('480'); self.gif_width.setFixedWidth(70)
        gif_lay.addWidget(self.gif_width)
        gif_lay.addSpacing(12)
        self._gif_warn_lbl = QLabel("※ GIF는 파일 크기가 클 수 있습니다.")
        gif_lay.addWidget(self._gif_warn_lbl)
        gif_lay.addStretch()
        self.gif_width.currentTextChanged.connect(self._on_gif_width_changed)
        self.gif_row.setVisible(False)
        bot_lay.addWidget(self.gif_row)

        # ── 영상 효과 옵션 ────────────────────────────────────────────────────
        self.effect_row = QWidget()
        eff_lay = QHBoxLayout(self.effect_row)
        eff_lay.setContentsMargins(0, 0, 0, 0)
        eff_lay.addWidget(QLabel("회전/반전:"))
        self.rot_combo = QComboBox()
        self.rot_combo.addItems(['없음', '90° 시계', '90° 반시계', '180°', '좌우 반전', '상하 반전'])
        self.rot_combo.setFixedWidth(110)
        eff_lay.addWidget(self.rot_combo)
        eff_lay.addSpacing(16)
        eff_lay.addWidget(QLabel("볼륨:"))
        self.vol_spin = QDoubleSpinBox()
        self.vol_spin.setRange(0.1, 5.0)
        self.vol_spin.setSingleStep(0.1)
        self.vol_spin.setValue(1.0)
        self.vol_spin.setDecimals(1)
        self.vol_spin.setFixedWidth(68)
        self.vol_spin.setToolTip("1.0 = 원본 볼륨  /  2.0 = 2배  /  0.5 = 절반")
        eff_lay.addWidget(self.vol_spin)
        eff_lay.addWidget(QLabel("x"))
        eff_lay.addSpacing(16)
        eff_lay.addWidget(QLabel("압축(CRF):"))
        self.crf_spin = QSpinBox()
        self.crf_spin.setRange(0, 51)
        self.crf_spin.setValue(0)
        self.crf_spin.setFixedWidth(54)
        self.crf_spin.setToolTip("0 = 자동(기본)  /  값이 클수록 용량↓ 화질↓  (권장: 18~28)")
        eff_lay.addWidget(self.crf_spin)
        eff_lay.addStretch()
        self.screenshot_btn = QPushButton("📷 스크린샷")
        self.screenshot_btn.setFixedWidth(100)
        self.screenshot_btn.setToolTip("현재 재생 위치를 PNG로 저장")
        self.screenshot_btn.clicked.connect(self._screenshot)
        eff_lay.addWidget(self.screenshot_btn)
        bot_lay.addWidget(self.effect_row)

        # ── Save path ─────────────────────────────────────────────────────────
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("저장 경로:"))
        self.path_input = QComboBox()
        self.path_input.setEditable(True)
        self.path_input.setInsertPolicy(QComboBox.NoInsert)
        self.path_input.lineEdit().setPlaceholderText("저장할 폴더 경로")
        self.path_input.addItem(DOWNLOAD_DIR)
        self.path_input.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self.path_input)
        btn_browse = QPushButton("폴더 선택")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self._browse_folder)
        path_row.addWidget(btn_browse)
        btn_open = QPushButton("열기")
        btn_open.setFixedWidth(50)
        btn_open.clicked.connect(self._open_folder)
        path_row.addWidget(btn_open)
        bot_lay.addLayout(path_row)

        # ── File name ─────────────────────────────────────────────────────────
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("파일 이름:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("비워두면 자동 생성  (확장자 제외)")
        name_row.addWidget(self.name_input)
        self.name_ext_lbl = QLabel(".mp4")
        self.name_ext_lbl.setFixedWidth(50)
        fb2 = QFont(); fb2.setBold(True)
        self.name_ext_lbl.setFont(fb2)
        name_row.addWidget(self.name_ext_lbl)
        bot_lay.addLayout(name_row)

        bot_lay.addWidget(self._hline())

        # ── Download / Cancel ─────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.dl_btn = QPushButton("구간 저장 (다운로드)")
        self.dl_btn.setFixedHeight(54)
        bf = QFont(); bf.setPointSize(13); bf.setBold(True)
        self.dl_btn.setFont(bf)
        self.dl_btn.clicked.connect(self.start_download)

        self.queue_add_btn = QPushButton("큐에 추가")
        self.queue_add_btn.setFixedHeight(54)
        self.queue_add_btn.setFixedWidth(150)
        bf_q = QFont(); bf_q.setPointSize(13); bf_q.setBold(True)
        self.queue_add_btn.setFont(bf_q)
        self.queue_add_btn.clicked.connect(self._add_to_queue)

        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.setFixedHeight(54)
        self.cancel_btn.setFixedWidth(90)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_download)

        self.autoopen_chk = QCheckBox("저장 후 즉시 실행")
        btn_row.addWidget(self.dl_btn)
        btn_row.addWidget(self.queue_add_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.autoopen_chk)
        bot_lay.addLayout(btn_row)

        # ── Progress bar ──────────────────────────────────────────────────────
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        bot_lay.addWidget(self.progress)

        self._left_splitter.setSizes([500, 460])
        self._left_splitter.setStretchFactor(0, 1)
        self._left_splitter.setStretchFactor(1, 1)

        # ── Right: 탭 위젯 (저장 폴더 / 로그) ───────────────────────────────────
        self._right_tabs = QTabWidget()
        f_small = QFont(); f_small.setPointSize(9)

        # ── Tab 1: 저장 폴더 ─────────────────────────────────────────────────
        folder_tab = QWidget()
        folder_lay = QVBoxLayout(folder_tab)
        folder_lay.setSpacing(6)
        folder_lay.setContentsMargins(6, 8, 6, 8)

        hdr = QHBoxLayout()
        self._folder_lbl = QLabel("저장 폴더 내용")
        self._folder_lbl.setFont(f_small)
        hdr.addWidget(self._folder_lbl)
        hdr.addStretch()
        btn_ref = QPushButton("새로고침")
        btn_ref.setFixedWidth(72)
        btn_ref.clicked.connect(self._refresh_file_list)
        hdr.addWidget(btn_ref)
        folder_lay.addLayout(hdr)

        path_row = QHBoxLayout()
        self._folder_path_lbl = QLabel(DOWNLOAD_DIR)
        self._folder_path_lbl.setFont(f_small)
        self._folder_path_lbl.setStyleSheet("color: #888;")
        self._folder_path_lbl.setWordWrap(False)
        self._folder_path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        path_row.addWidget(self._folder_path_lbl)
        btn_open_folder = QPushButton("폴더 열기")
        btn_open_folder.setFixedWidth(72)
        btn_open_folder.clicked.connect(self._open_save_folder)
        path_row.addWidget(btn_open_folder)
        folder_lay.addLayout(path_row)

        self.file_tree = FileTree()
        self.file_tree.setColumnCount(5)
        self.file_tree.setHeaderLabels(["이름", "확장자", "크기", "날짜", "♪"])
        self.file_tree.setAlternatingRowColors(True)
        self.file_tree.setFont(f_small)
        self.file_tree.setDragEnabled(True)
        self.file_tree.setAcceptDrops(True)
        self.file_tree.setDropIndicatorShown(True)
        self.file_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        from PyQt5.QtWidgets import QAbstractItemView as _AIV
        self.file_tree.setSelectionMode(_AIV.ExtendedSelection)
        from PyQt5.QtWidgets import QHeaderView as _HV
        self.file_tree.header().setStretchLastSection(False)
        self.file_tree.header().setSectionResizeMode(0, _HV.Interactive)
        self.file_tree.header().setSectionResizeMode(1, _HV.Interactive)
        self.file_tree.header().setSectionResizeMode(2, _HV.Interactive)
        self.file_tree.header().setSectionResizeMode(3, _HV.Interactive)
        self.file_tree.header().setSectionResizeMode(4, _HV.Fixed)
        self.file_tree.setColumnWidth(0, 160)
        self.file_tree.setColumnWidth(1, 50)
        self.file_tree.setColumnWidth(2, 65)
        self.file_tree.setColumnWidth(3, 125)
        self.file_tree.setColumnWidth(4, 32)
        self.file_tree.header().setSectionsClickable(True)
        self.file_tree.header().setSortIndicatorShown(True)
        self.file_tree.header().setSortIndicator(0, Qt.AscendingOrder)
        self.file_tree.header().sectionClicked.connect(self._on_tree_header_clicked)
        self.file_tree.itemDoubleClicked.connect(self._on_tree_double_click)
        self.file_tree.itemExpanded.connect(self._on_tree_expand)
        self.file_tree.customContextMenuRequested.connect(self._on_tree_context)
        self.file_tree.itemChanged.connect(self._on_tree_item_changed)
        self.file_tree.itemClicked.connect(self._on_tree_item_clicked)
        self.file_tree.f2Pressed.connect(self._start_rename)
        self.file_tree.deletePressed.connect(self._tree_delete_current)
        self.file_tree.enterPressed.connect(self._tree_open_current)
        self.file_tree.fileMoved.connect(
            lambda s, d: self._log(f"이동: {os.path.basename(s)} → {os.path.relpath(os.path.dirname(d), self.path_input.currentText().strip() or DOWNLOAD_DIR)}/"))
        self.file_tree.moveError.connect(lambda e: self._log(f"이동 실패: {e}"))
        folder_lay.addWidget(self.file_tree)

        self._right_tabs.addTab(folder_tab, "저장 폴더")

        # ── 저장 큐 (탭 아래 고정 영역) ──────────────────────────────────────
        queue_panel = QWidget()
        queue_tab_lay = QVBoxLayout(queue_panel)
        queue_tab_lay.setContentsMargins(6, 4, 6, 4)
        queue_tab_lay.setSpacing(4)

        q_splitter = QSplitter(Qt.Vertical)

        # ── 영상 큐 (상단) ──
        vid_q_w = QWidget()
        vid_q_lay = QVBoxLayout(vid_q_w)
        vid_q_lay.setContentsMargins(0, 0, 0, 0)
        vid_q_lay.setSpacing(4)

        vid_q_hdr = QHBoxLayout()
        vid_q_lbl = QLabel("🎬 영상 큐")
        vid_q_lbl.setFont(f_small)
        vid_q_hdr.addWidget(vid_q_lbl)
        vid_q_hdr.addSpacing(6)
        self._queue_run_btn = QPushButton("큐 실행")
        self._queue_run_btn.setFixedWidth(72)
        self._queue_run_btn.clicked.connect(self._run_queue)
        vid_q_hdr.addWidget(self._queue_run_btn)
        self._queue_clear_btn = QPushButton("큐 초기화")
        self._queue_clear_btn.setFixedWidth(72)
        self._queue_clear_btn.clicked.connect(self._clear_queue)
        vid_q_hdr.addWidget(self._queue_clear_btn)
        vid_q_hdr.addStretch()
        self._queue_merge_chk = QCheckBox("구간 합치기")
        vid_q_hdr.addWidget(self._queue_merge_chk)
        vid_q_lay.addLayout(vid_q_hdr)

        self.queue_tree = QTreeWidget()
        self.queue_tree.setColumnCount(7)
        self.queue_tree.setHeaderLabels(["#", "URL/파일", "구간", "길이", "예상크기", "형식", "상태"])
        self.queue_tree.setAlternatingRowColors(True)
        self.queue_tree.setFont(f_small)
        self.queue_tree.setColumnWidth(0, 30)
        self.queue_tree.setColumnWidth(1, 200)
        self.queue_tree.setColumnWidth(2, 100)
        self.queue_tree.setColumnWidth(3, 55)
        self.queue_tree.setColumnWidth(4, 65)
        self.queue_tree.setColumnWidth(5, 55)
        self.queue_tree.setColumnWidth(6, 70)
        self.queue_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_tree.customContextMenuRequested.connect(self._on_queue_context)
        self.queue_tree.itemDoubleClicked.connect(self._on_queue_item_double_click)
        self.queue_tree.keyPressEvent = self._queue_tree_key_press
        vid_q_lay.addWidget(self.queue_tree)
        q_splitter.addWidget(vid_q_w)

        # ── GIF 큐 (하단) ──
        gif_q_w = QWidget()
        gif_q_lay = QVBoxLayout(gif_q_w)
        gif_q_lay.setContentsMargins(0, 0, 0, 0)
        gif_q_lay.setSpacing(4)

        gif_q_hdr = QHBoxLayout()
        gif_q_lbl = QLabel("🖼 GIF 큐")
        gif_q_lbl.setFont(f_small)
        gif_q_hdr.addWidget(gif_q_lbl)
        gif_q_hdr.addSpacing(6)
        self._gif_queue_run_btn = QPushButton("큐 실행")
        self._gif_queue_run_btn.setFixedWidth(72)
        self._gif_queue_run_btn.clicked.connect(self._run_gif_queue)
        gif_q_hdr.addWidget(self._gif_queue_run_btn)
        self._gif_queue_clear_btn = QPushButton("큐 초기화")
        self._gif_queue_clear_btn.setFixedWidth(72)
        self._gif_queue_clear_btn.clicked.connect(self._clear_gif_queue)
        gif_q_hdr.addWidget(self._gif_queue_clear_btn)
        gif_q_hdr.addStretch()
        self._gif_queue_merge_chk = QCheckBox("구간 합치기")
        gif_q_hdr.addWidget(self._gif_queue_merge_chk)
        gif_q_lay.addLayout(gif_q_hdr)

        self.gif_queue_tree = QTreeWidget()
        self.gif_queue_tree.setColumnCount(7)
        self.gif_queue_tree.setHeaderLabels(["#", "URL/파일", "구간", "길이", "예상크기", "설정", "상태"])
        self.gif_queue_tree.setAlternatingRowColors(True)
        self.gif_queue_tree.setFont(f_small)
        self.gif_queue_tree.setColumnWidth(0, 30)
        self.gif_queue_tree.setColumnWidth(1, 200)
        self.gif_queue_tree.setColumnWidth(2, 100)
        self.gif_queue_tree.setColumnWidth(3, 55)
        self.gif_queue_tree.setColumnWidth(4, 65)
        self.gif_queue_tree.setColumnWidth(5, 75)
        self.gif_queue_tree.setColumnWidth(6, 70)
        self.gif_queue_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.gif_queue_tree.customContextMenuRequested.connect(self._on_gif_queue_context)
        self.gif_queue_tree.itemDoubleClicked.connect(self._on_gif_queue_item_double_click)
        self.gif_queue_tree.keyPressEvent = self._gif_queue_tree_key_press
        gif_q_lay.addWidget(self.gif_queue_tree)
        q_splitter.addWidget(gif_q_w)

        q_splitter.setSizes([300, 200])
        queue_tab_lay.addWidget(q_splitter)

        # ── Tab 2: 로그 ──────────────────────────────────────────────────────
        log_tab = QWidget()
        log_lay = QVBoxLayout(log_tab)
        log_lay.setContentsMargins(6, 8, 6, 8)
        self.log_box = QTextBrowser()
        self.log_box.setReadOnly(True)
        self.log_box.setFont(f_small)
        self.log_box.setOpenLinks(False)
        self.log_box.setPlaceholderText("다운로드 로그가 여기에 표시됩니다.")
        self.log_box.anchorClicked.connect(self._on_log_link_clicked)
        self.log_box.setContextMenuPolicy(Qt.CustomContextMenu)
        self.log_box.customContextMenuRequested.connect(self._on_log_context_menu)
        log_lay.addWidget(self.log_box)
        self._right_tabs.addTab(log_tab, "로그")

        # ── 오른쪽 패널: 탭(상단) + 저장 큐(하단) 세로 스플리터 ───────────────
        self._right_vsplitter = QSplitter(Qt.Vertical)
        self._right_vsplitter.addWidget(self._right_tabs)
        self._right_vsplitter.addWidget(queue_panel)
        self._right_vsplitter.setSizes([400, 260])
        self._right_vsplitter.setStretchFactor(0, 6)
        self._right_vsplitter.setStretchFactor(1, 4)

        splitter.addWidget(self._right_vsplitter)
        splitter.setSizes([730, 320])
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        self.path_input.lineEdit().editingFinished.connect(self._refresh_file_list)
        self.path_input.currentIndexChanged.connect(self._refresh_file_list)
        self.path_input.lineEdit().editingFinished.connect(self._update_folder_path_lbl)
        self.path_input.currentIndexChanged.connect(self._update_folder_path_lbl)
        QTimer.singleShot(200, self._refresh_file_list)

        # ── 메뉴바: 도구 ─────────────────────────────────────────────────────
        tool_menu = self.menuBar().addMenu("도구(&T)")
        tool_menu.addAction("기능 가이드", self._show_feature_guide)
        tool_menu.addSeparator()
        tool_menu.addAction("쿠키 파일 설정...", self._set_cookie_file)
        tool_menu.addSeparator()
        tool_menu.addAction("yt-dlp 업데이트", self._update_ytdlp)
        tool_menu.addAction("ffmpeg 업데이트 방법...", self._show_ffmpeg_update_help)
        tool_menu.addSeparator()
        tool_menu.addAction("프로그램 업데이트 확인", self._check_update_manual)

    def _hline(self):
        f = QFrame(); f.setFrameShape(QFrame.HLine); f.setFrameShadow(QFrame.Sunken)
        return f

    def _setup_timers(self):
        self._poll_timer = QTimer()
        self._poll_timer.setInterval(400)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()

        self._dur_timer = QTimer()
        self._dur_timer.setInterval(1500)
        self._dur_timer.timeout.connect(self._poll_dur)

    # -----------------------------------------------------------------------
    # Version fetch (yt-dlp / ffmpeg)
    # -----------------------------------------------------------------------
    def _fetch_versions(self):
        self.statusBar().showMessage("yt-dlp: 확인 중...  |  ffmpeg: 확인 중...")
        self._ver_ytdlp = QProcess(self)
        self._ver_ytdlp.setProcessChannelMode(QProcess.MergedChannels)
        self._ver_ytdlp.finished.connect(self._on_ytdlp_ver)
        self._ver_ytdlp.start(YTDLP_EXE, ['--version'])

    def _on_ytdlp_ver(self):
        out = self._ver_ytdlp.readAll().data().decode('utf-8', errors='replace').strip()
        self._ytdlp_ver_str = out or '?'
        self._ver_ffmpeg = QProcess(self)
        self._ver_ffmpeg.setProcessChannelMode(QProcess.MergedChannels)
        self._ver_ffmpeg.finished.connect(self._on_ffmpeg_ver)
        self._ver_ffmpeg.start(FFMPEG_EXE, ['-version'])

    def _on_ffmpeg_ver(self):
        out = self._ver_ffmpeg.readAll().data().decode('utf-8', errors='replace')
        m = re.search(r'ffmpeg version ([\S]+)', out)
        ffver = m.group(1) if m else '?'
        self.statusBar().showMessage(
            f"yt-dlp {self._ytdlp_ver_str}  |  ffmpeg {ffver}")

    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # Slider 초기화 헬퍼
    # -----------------------------------------------------------------------
    def _reset_slider(self, dur: int):
        """새 파일/URL 등록 시 슬라이더를 [0, dur] 로 초기화."""
        self.rslider.setRange(0, dur)
        self._upd_start = True
        self.rslider.setStart(0, silent=True)
        self.start_in.setText(secs_to_hms(0))
        self._upd_start = False
        self._upd_end = True
        self.rslider.setEnd(dur, silent=True)
        self.end_in.setText(secs_to_hms(dur))
        self._upd_end = False
        self._refresh_dur()
        # 큐 더블클릭으로 지정된 구간이 있으면 복원
        if self._pending_segment:
            ps, pe = self._pending_segment
            self._pending_segment = None
            ps = max(0, min(ps, dur))
            pe = max(ps + 1, min(pe, dur))
            self._upd_start = True
            self.rslider.setStart(ps, silent=True)
            self.start_in.setText(secs_to_hms(ps))
            self._upd_start = False
            self._upd_end = True
            self.rslider.setEnd(pe, silent=True)
            self.end_in.setText(secs_to_hms(pe))
            self._upd_end = False
            self._refresh_dur()
            if self._local_is_gif:
                self._pending_gif_seek = True   # 프록시 로드 완료 후 seek+play
            else:
                QTimer.singleShot(200, self._seek_start_and_play)

    # -----------------------------------------------------------------------
    # Always on top
    # -----------------------------------------------------------------------
    def _on_pin_toggled(self, checked: bool):
        flags = self.windowFlags()
        if checked:
            self.setWindowFlags(flags | Qt.WindowStaysOnTopHint)
            self._pin_btn.setText("📌 고정 중")
        else:
            self.setWindowFlags(flags & ~Qt.WindowStaysOnTopHint)
            self._pin_btn.setText("📌 항상 위")
        self.show()  # setWindowFlags 후 창 재표시 필요

    # Source mode switch
    # -----------------------------------------------------------------------
    def _set_source(self, idx: int):
        """토글 버튼 클릭 → 버튼 상태 동기화 후 _on_source_changed 호출."""
        self._src_url_btn.setChecked(idx == 0)
        self._src_local_btn.setChecked(idx == 1)
        self._on_source_changed(idx)

    def _on_source_changed(self, idx: int):
        self._mode = 'local' if idx == 1 else 'url'
        self.url_widget.setVisible(self._mode == 'url')
        self.local_widget.setVisible(self._mode == 'local')
        self.res_combo.setEnabled(
            self.ext_combo.currentText() not in ('mp3 (오디오만)', 'gif'))
        # 로컬 모드로 전환 시 YouTube 플레이어 복원
        if self._mode == 'url':
            self._yt_loaded = False
            self._local_is_gif = False
            self.ext_combo.setEnabled(True)
            self.gif_fps.setEnabled(True)
            self.gif_width.setEnabled(True)
            self.dl_btn.setText("구간 저장 (다운로드)")
            if not self._queue_running:
                self.queue_add_btn.setEnabled(True)
            player_url = f"http://127.0.0.1:{_SERVER_PORT}/"
            current = self.web.url().toString()
            if current.rstrip('/') != player_url.rstrip('/'):
                self.web.setUrl(QUrl(player_url))
        self._update_overlay()

    # -----------------------------------------------------------------------
    # Local file browse & preview
    # -----------------------------------------------------------------------
    def _browse_local_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 파일 선택", "",
            "동영상/GIF (*.mp4 *.mkv *.avi *.mov *.webm *.flv *.wmv *.m4v *.gif);;모든 파일 (*.*)")
        if path:
            self._local_file = path
            self._proxy_loaded = False   # 새 파일 → 프록시 상태 초기화
            self.local_path_lbl.setText(path)
            self._load_local_preview(path)
            self.name_input.setText(os.path.splitext(os.path.basename(path))[0])
            self._apply_local_gif_mode(path)

    def _load_local_preview(self, path: str):
        """로컬 영상/GIF 를 웹뷰에 로드."""
        self._yt_loaded = True
        self._web_container.hide_overlay()

        if path.lower().endswith('.gif'):
            # GIF는 <img>로 즉시 표시 후 백그라운드에서 WebM 프록시 변환
            filename = os.path.basename(path)
            encoded_filename = _url_quote(filename, safe='')
            base_url = QUrl.fromLocalFile(os.path.dirname(path) + os.sep)
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* {{margin:0;padding:0;box-sizing:border-box;}}
html,body {{width:100%;height:100%;background:#000;overflow:hidden;display:flex;
           align-items:center;justify-content:center;}}
img {{max-width:100%;max-height:100%;object-fit:contain;}}
</style></head><body>
<img src="{encoded_filename}">
<script>
function getCurrentTime()  {{ return 0; }}
function getDuration()     {{ return 0; }}
function seekTo(s)         {{}}
function setRate(r)        {{}}
function getPlaybackRate() {{ return 1; }}
function getVideoError()   {{ return null; }}
</script>
</body></html>"""
            self.web.setHtml(html, base_url)
            # duration은 _probe_gif_properties 에서 nb_frames/fps 로 계산
            # 백그라운드에서 WebM 프록시 변환 시작
            self._create_gif_preview_proxy()
        else:
            # 비디오: 로컬 HTTP 서버로 서빙 (file:// 보안 제한 · CORS 우회)
            global _LOCAL_VIDEO_PATH, _LOCAL_VIDEO_HTML
            _LOCAL_VIDEO_PATH = path
            is_gif_proxy = self._gif_proxy_mode
            self._gif_proxy_mode = False
            autoplay_attr = 'autoplay' if is_gif_proxy else ''
            video_url = f"http://127.0.0.1:{_SERVER_PORT}/video"
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* {{margin:0;padding:0;box-sizing:border-box;}}
html,body {{width:100%;height:100%;background:#000;overflow:hidden;}}
video {{width:100%;height:100%;}}
#err {{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
       color:#ff6b6b;background:rgba(0,0,0,.8);padding:14px 18px;border-radius:6px;
       font-size:13px;text-align:center;max-width:80%;}}
</style></head><body>
<video id="v" src="{video_url}" controls {autoplay_attr} preload="metadata"></video>
<div id="err"></div>
<script>
var v = document.getElementById('v');
var errDiv = document.getElementById('err');
window._videoError = null;
v.onerror = function() {{
  var msg = v.error ? ('코드 ' + v.error.code + ': ' + (v.error.message || '재생 불가')) : '알 수 없는 오류';
  window._videoError = msg;
  errDiv.style.display = 'block';
  errDiv.textContent = '⚠ 미리보기 불가  (' + msg + ')\\n다운로드/구간 추출은 정상 동작합니다.';
}};
function getCurrentTime()  {{ return v.currentTime || 0; }}
function getDuration()     {{ return isNaN(v.duration) ? 0 : v.duration; }}
function seekTo(s)         {{ var p=!v.paused; v.currentTime=s; if(p) v.play().catch(function(){{}}); }}
function setRate(r)        {{ v.playbackRate = r; }}
function getPlaybackRate() {{ return v.playbackRate || 1; }}
function getVideoError()   {{ return window._videoError; }}
function togglePlay()      {{ if(v.paused) v.play().catch(function(){{}}); else v.pause(); }}
v.addEventListener('click', function() {{ togglePlay(); }});
</script>
</body></html>"""
            self._video_token = getattr(self, '_video_token', 0) + 1
            _LOCAL_VIDEO_HTML = html.encode('utf-8')
            local_player_url = f"http://127.0.0.1:{_SERVER_PORT}/local_player"
            current_url = self.web.url().toString()
            if current_url.startswith(local_player_url.split('?')[0]):
                # 이미 /local_player 페이지 — JS로 video src만 교체 (빠른 전환)
                new_src = f"http://127.0.0.1:{_SERVER_PORT}/video?t={self._video_token}"
                js = (f"window._videoError=null;"
                      f"document.getElementById('err').style.display='none';"
                      f"var v=document.getElementById('v');"
                      f"v.src='{new_src}';"
                      f"v.load();"
                      + ("v.play().catch(function(){});" if autoplay_attr else ""))
                self.web.page().runJavaScript(js)
            else:
                self.web.setUrl(QUrl(local_player_url))
            if not is_gif_proxy:  # GIF 프록시는 duration 이미 확정 — 재폴링 불필요
                self._dur_timer.start()
            QTimer.singleShot(3000, self._check_video_error)
        if abs(self._speed - 1.0) > 0.001:
            QTimer.singleShot(500, lambda: self.web.page().runJavaScript(
                f"setRate({self._speed});"))

    def _check_video_error(self):
        """로컬 미리보기 로드 3초 후 오류 여부 확인 (프록시 파일이면 건너뜀)."""
        if self._mode != 'local' or not self._yt_loaded:
            return
        if self._local_is_gif:
            return  # GIF는 WebM 프록시로 처리 — 오디오 전용 프록시 생성 방지
        if getattr(self, '_proxy_loaded', False):
            return  # 이미 프록시로 성공 로드됨 — 재귀 방지
        self.web.page().runJavaScript("getVideoError();", self._on_video_error_result)

    def _on_video_error_result(self, err):
        if err:
            self._log(f"⚠ 미리보기 호환 불가 포맷 ({err}) — VP8/WebM 변환 중...")
            self._web_container.show_overlay("⏳  미리보기 변환 중...")
            self._create_preview_proxy()
        else:
            self._log("✔ 미리보기 정상 재생 중")

    def _create_gif_preview_proxy(self):
        """GIF → WebM(VP8) 변환 (구간 재생 · 반복 재생 지원용 프록시).
        MP4/H.264 대신 WebM 사용 — QtWebEngine Chromium은 VP8을 항상 지원.
        """
        if not self._local_file:
            return
        # 이전 프록시 변환 프로세스가 남아있으면 종료
        old_proc = getattr(self, '_proxy_proc', None)
        if old_proc and old_proc.state() != QProcess.NotRunning:
            try:
                old_proc.finished.disconnect()
            except Exception:
                pass
            old_proc.kill()
            old_proc.waitForFinished(1000)
        # 이전 프록시 파일 삭제 (브라우저 캐시 방지를 위해 매번 고유 경로 사용)
        old_proxy = getattr(self, '_preview_proxy', '')
        if old_proxy and os.path.isfile(old_proxy):
            try:
                os.remove(old_proxy)
            except Exception:
                pass
        self._proxy_counter += 1
        proxy_path = os.path.join(BASE_DIR, f'_preview_proxy_{os.getpid()}_{self._proxy_counter}.webm')
        self._preview_proxy = proxy_path
        self._proxy_source = self._local_file   # 어느 파일용 프록시인지 기록
        self._gif_proxy_mode = True   # _on_proxy_done → _load_local_preview 에서 autoplay 적용
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda exit_code, _status: self._on_proxy_done(exit_code))
        proc.start(FFMPEG_EXE, [
            '-y', '-i', self._local_file,
            '-c:v', 'libvpx',
            '-b:v', '1M',
            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
            '-an',
            '-auto-alt-ref', '0',   # VP8 필수
            proxy_path,
        ])
        self._proxy_proc = proc

    def _create_preview_proxy(self):
        """비디오를 VP8/Vorbis WebM으로 재인코딩 — Chromium이 항상 지원하는 포맷."""
        if not self._local_file:
            return
        self._proxy_counter += 1
        proxy_path = os.path.join(
            BASE_DIR, f'_preview_proxy_{os.getpid()}_{self._proxy_counter}.webm')
        self._preview_proxy = proxy_path
        self._proxy_source  = self._local_file
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda exit_code, _status: self._on_proxy_done(exit_code))
        proc.start(FFMPEG_EXE, [
            '-y', '-i', self._local_file,
            '-map', '0:v:0',           # 첫 번째 비디오 스트림
            '-map', '0:a:0?',          # 오디오 (없으면 건너뜀)
            '-c:v', 'libvpx',
            '-deadline', 'realtime',   # 실시간 인코딩 (가장 빠름)
            '-cpu-used', '8',          # 최대 속도 (0=고화질, 8=고속)
            '-b:v', '1M',
            '-vf', 'scale=-2:480',     # 480p — 미리보기용 (720p 대비 2배 빠름)
            '-pix_fmt', 'yuv420p',
            '-auto-alt-ref', '0',      # VP8 필수
            '-c:a', 'libvorbis', '-b:a', '96k',
            '-map_metadata', '-1',
            proxy_path])
        self._proxy_proc = proc

    def _on_proxy_done(self, exit_code=0):
        proxy = getattr(self, '_preview_proxy', '')
        # 이미 다른 파일이 로드된 경우 (프록시 완료 전에 새 파일 선택) 무시
        if getattr(self, '_proxy_source', None) != self._local_file:
            return
        if exit_code != 0:
            # ffmpeg 실패 — stderr 로그 출력
            proc = getattr(self, '_proxy_proc', None)
            if proc:
                err_out = proc.readAll().data().decode('utf-8', errors='replace').strip()
                if err_out:
                    self._log(f"[ffmpeg] {err_out[-500:]}")  # 마지막 500자만
            self._log("✘ 미리보기 변환 실패 — 슬라이더 범위만 설정합니다.")
            self._web_container.show_overlay("⚠  미리보기 불가")
            self._probe_local_duration()
            return
        if proxy and os.path.isfile(proxy):
            self._log("✔ 미리보기 변환 완료")
            self._proxy_loaded = True   # 에러 체크 루프 방지
            self._load_local_preview(proxy)
            if self._pending_gif_seek:
                self._pending_gif_seek = False
                QTimer.singleShot(500, self._seek_start_and_play)
        else:
            self._log("✘ 미리보기 변환 실패 — 슬라이더 범위만 설정합니다.")
            self._web_container.show_overlay("⚠  미리보기 불가")
            self._probe_local_duration()

    def _probe_local_duration(self):
        """ffprobe로 로컬 파일 재생 시간을 가져와 슬라이더 범위를 설정."""
        if not self._local_file:
            return
        ffprobe = os.path.join(FFMPEG_DIR, 'ffprobe.exe')
        if not os.path.isfile(ffprobe):
            # ffprobe 없으면 ffmpeg -i 로 stderr 파싱
            self._probe_with_ffmpeg()
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda *_, p=proc: self._on_probe_done(p))
        proc.start(ffprobe, [
            '-v', 'quiet', '-show_entries', 'format=duration',
            '-of', 'csv=p=0', self._local_file])
        self._probe_proc = proc

    def _on_probe_done(self, proc: QProcess):
        out = proc.readAll().data().decode('utf-8', errors='replace').strip()
        try:
            dur = float(out.split()[0])
            if dur > 0:
                self._duration = int(dur)
                self._reset_slider(self._duration)
                self._update_size_estimate()
        except (ValueError, IndexError):
            pass

    def _probe_with_ffmpeg(self):
        """ffprobe 없을 때 ffmpeg -i stderr에서 Duration 파싱."""
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda *_, p=proc: self._on_ffmpeg_probe_done(p))
        proc.start(FFMPEG_EXE, ['-i', self._local_file])
        self._probe_proc = proc

    def _on_ffmpeg_probe_done(self, proc: QProcess):
        out = proc.readAll().data().decode('utf-8', errors='replace')
        m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', out)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if dur > 0:
                self._duration = int(dur)
                self._reset_slider(self._duration)
                self._update_size_estimate()

    # -----------------------------------------------------------------------
    # Video loading (URL mode)
    # -----------------------------------------------------------------------
    def load_video(self):
        url = (self.url_input.currentData() or self.url_input.currentText()).strip()
        if not url:
            return
        self._add_recent_url(url)

        if is_youtube(url):
            vid = extract_yt_id(url)
            if not vid:
                self._log("유효한 YouTube URL이 아닙니다.")
                return
            player_url = f"http://127.0.0.1:{_SERVER_PORT}/"
            if self.web.url().toString().startswith(player_url):
                # 플레이어 페이지가 이미 로드된 상태 → 바로 재생
                self.web.page().runJavaScript(f"loadVideo('{vid}');")
                self._yt_loaded = True
                self._web_container.hide_overlay()
                self._dur_timer.start()
                QTimer.singleShot(2500, self._fetch_yt_title)
                QTimer.singleShot(4000, self._check_yt_embed_error)
                if abs(self._speed - 1.0) > 0.001:
                    QTimer.singleShot(3000, lambda: self.web.page().runJavaScript(
                        f"setRate({self._speed});"))
            else:
                # placeholder로 바꿔진 상태 → 플레이어 페이지 먼저 복원
                self._web_container.hide_overlay()
                self._pending_yt_id = vid
                self.web.setUrl(QUrl(player_url))
        else:
            self._yt_loaded = False
            self._pending_yt_id = None
            self._web_container.show_overlay("🔍 영상 정보 가져오는 중...")
            self._log("영상 정보 조회 중 (yt-dlp)...")
            self._start_url_preview(url)

    def _on_page_loaded(self, ok: bool):
        """플레이어 페이지 복원 완료 후 대기 중인 YouTube ID를 재생."""
        if self._mode == 'url' and self._pending_yt_id:
            vid = self._pending_yt_id
            self._pending_yt_id = None
            self.web.page().runJavaScript(f"loadVideo('{vid}');")
            self._yt_loaded = True
            self._web_container.hide_overlay()
            self._dur_timer.start()
            QTimer.singleShot(2500, self._fetch_yt_title)
            QTimer.singleShot(4000, self._check_yt_embed_error)
            if abs(self._speed - 1.0) > 0.001:
                QTimer.singleShot(3000, lambda: self.web.page().runJavaScript(
                    f"setRate({self._speed});"))

    def _fetch_yt_title(self):
        self.web.page().runJavaScript("getVideoTitle();", self._apply_video_title)

    def _apply_video_title(self, title: str):
        if title and title.strip():
            self.name_input.setText(title.strip())
            url = self.url_input.currentData() or self.url_input.currentText().strip()
            self._update_recent_url_title(url, title.strip())

    def _check_yt_embed_error(self):
        """YouTube 로드 4초 후 embed 차단 오류 여부 확인."""
        if not self._yt_loaded or self._mode != 'url':
            return
        self.web.page().runJavaScript("getYTError();", self._on_yt_error_result)

    def _on_yt_error_result(self, code):
        if code in (101, 150):
            self.web.page().runJavaScript("clearYTError();")
            self._web_container.show_overlay("⚠  이 영상은 미리보기가 지원되지 않습니다\n구간을 직접 입력 후 저장하세요")
            self._log("⚠ 이 영상은 삽입(embed)이 차단되어 미리보기가 불가합니다. 구간 입력 후 저장은 정상 동작합니다.")

    # -----------------------------------------------------------------------
    # Non-YouTube URL preview (Instagram / Facebook / etc.)
    # -----------------------------------------------------------------------
    def _start_url_preview(self, url: str):
        """yt-dlp -j 로 스트림 URL 추출 후 HTML5 플레이어로 미리보기."""
        # 이전 조회 프로세스가 있으면 중단
        if self._url_info_proc and self._url_info_proc.state() != QProcess.NotRunning:
            self._url_info_proc.kill()
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda *_, p=proc, u=url: self._on_url_info_done(p, u))
        proc.start(YTDLP_EXE, ['-j', '--no-playlist', url])
        self._url_info_proc = proc

    def _on_url_info_done(self, proc: QProcess, orig_url: str):
        out = proc.readAll().data().decode('utf-8', errors='replace').strip()
        # JSON 파싱 (마지막 줄만 사용 — 일부 플랫폼이 progress 출력 먼저 함)
        info = None
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith('{'):
                try:
                    info = json.loads(line)
                    break
                except Exception:
                    continue
        if not info:
            self._log("영상 정보를 가져오지 못했습니다. 다운로드는 정상 동작합니다.")
            self._web_container.show_overlay("⚠ 미리보기 불가 — 다운로드는 가능합니다")
            return

        title = info.get('title') or ''
        if title:
            self.name_input.setText(title.strip())
            url = self.url_input.currentData() or self.url_input.currentText().strip()
            self._update_recent_url_title(url, title.strip())

        dur = info.get('duration')
        if dur:
            self._duration = int(dur)
            self._reset_slider(self._duration)
            self._update_size_estimate()

        stream_url, is_hls = self._pick_stream_url(info)
        if not stream_url:
            self._log("직접 재생 가능한 스트림 없음 — 다운로드는 가능합니다.")
            self._web_container.show_overlay("⚠ 미리보기 불가 — 다운로드는 가능합니다")
            return

        if is_hls:
            self._log("HLS 스트림 — 미리보기 불가. 구간 직접 입력 후 저장하세요.")
            self._web_container.show_overlay("⚠ 미리보기 불가 (HLS 스트림)\n구간을 직접 입력 후 저장하세요")
            self._yt_loaded = True  # 슬라이더/저장 버튼 활성화
            return

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box;}}
html,body{{width:100%;height:100%;background:#000;overflow:hidden;}}
video{{width:100%;height:100%;}}
#err{{display:none;position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
      color:#ff6b6b;background:rgba(0,0,0,.8);padding:14px 18px;border-radius:6px;
      font-size:13px;text-align:center;max-width:80%;}}
</style></head><body>
<video id="v" src="{stream_url}" controls preload="metadata" crossorigin="anonymous"></video>
<div id="err"></div>
<script>
var v=document.getElementById('v');
var errDiv=document.getElementById('err');
window._videoError=null;
v.onerror=function(){{
  var msg=v.error?('코드 '+v.error.code+': '+(v.error.message||'재생 불가')):'알 수 없는 오류';
  window._videoError=msg;errDiv.style.display='block';
  errDiv.textContent='⚠ 미리보기 불가  ('+msg+')';
}};
function getCurrentTime(){{return v.currentTime||0;}}
function getDuration(){{return isNaN(v.duration)?0:v.duration;}}
function seekTo(s){{var p=!v.paused;v.currentTime=s;if(p)v.play().catch(function(){{}});}}
function setRate(r){{v.playbackRate=r;}}
function getPlaybackRate(){{return v.playbackRate||1;}}
function getVideoError(){{return window._videoError;}}
function togglePlay(){{if(v.paused)v.play().catch(function(){{}});else v.pause();}}
v.addEventListener('click',function(){{togglePlay();}});
</script>
</body></html>"""
        self.web.setHtml(html)
        self._yt_loaded = True
        self._web_container.hide_overlay()
        if not dur:
            self._dur_timer.start()
        self._log(f"미리보기 준비 완료: {title or orig_url}")
        if abs(self._speed - 1.0) > 0.001:
            QTimer.singleShot(500, lambda: self.web.page().runJavaScript(
                f"setRate({self._speed});"))

    def _pick_stream_url(self, info: dict) -> tuple:
        """formats 에서 직접 재생 가능한 스트림 URL 선택.
        반환: (url, is_hls) — HLS(m3u8) 여부를 함께 반환."""
        formats = info.get('formats') or []

        # 1순위: 직접 재생 가능한 MP4/WebM
        direct = []
        for f in formats:
            u = f.get('url', '')
            if not u:
                continue
            proto = f.get('protocol', '')
            if proto in ('m3u8', 'm3u8_native', 'dash'):
                continue
            if f.get('vcodec', 'none') == 'none':
                continue
            ext = f.get('ext', '')
            if ext in ('mp4', 'webm', 'mov', 'ts') or ext == '':
                direct.append(f)

        if not direct:
            direct = [f for f in formats
                      if f.get('url') and f.get('vcodec', 'none') != 'none'
                      and f.get('protocol', '') not in ('m3u8', 'm3u8_native', 'dash')]

        if direct:
            under720 = [f for f in direct if (f.get('height') or 9999) <= 720]
            pool = under720 if under720 else direct
            pool.sort(key=lambda f: (f.get('height') or 0), reverse=True)
            return pool[0].get('url', ''), False

        # 2순위: HLS 스트림 (네이버 TV 등)
        hls = [f for f in formats
               if f.get('url') and f.get('protocol', '') in ('m3u8', 'm3u8_native')
               and f.get('vcodec', 'none') != 'none']
        if hls:
            under720 = [f for f in hls if (f.get('height') or 9999) <= 720]
            pool = under720 if under720 else hls
            pool.sort(key=lambda f: (f.get('height') or 0), reverse=True)
            return pool[0].get('url', ''), True

        fallback = info.get('url', '')
        return fallback, False

    # -----------------------------------------------------------------------
    # Duration polling
    # -----------------------------------------------------------------------
    def _poll_dur(self):
        self.web.page().runJavaScript("getDuration();", self._apply_dur)

    def _apply_dur(self, val):
        if val and val > 1:
            self._duration = int(val)
            self._reset_slider(self._duration)
            self._dur_timer.stop()
            self._update_size_estimate()

    # -----------------------------------------------------------------------
    # Current time polling + loop
    # -----------------------------------------------------------------------
    def _poll(self):
        if self._yt_loaded:
            self.web.page().runJavaScript("getCurrentTime();", self._on_cur_time)

    def _on_cur_time(self, val):
        if val is None:
            return
        self.cur_lbl.setText(f"현재 위치:  {secs_to_hms(val)}")
        if self.loop_chk.isChecked():
            if val >= self.rslider.end - 0.3:
                start = self.rslider.start
                if self._local_is_gif and self._mode == 'local':
                    # GIF 프록시: <video>가 끝에서 멈출 수 있으므로 seek 후 명시적 재생
                    self.web.page().runJavaScript(
                        f"(function(){{var v=document.getElementById('v');"
                        f"if(v){{v.currentTime={start};v.play().catch(function(){{}});}}"
                        f"else seekTo({start});}})();"
                    )
                else:
                    self.web.page().runJavaScript(f"seekTo({start});")

    # -----------------------------------------------------------------------
    # Slider ↔ text sync
    # -----------------------------------------------------------------------
    def _on_slider_start(self, v: int):
        if self._upd_start: return
        self._upd_start = True
        self.start_in.setText(secs_to_hms(v))
        self._upd_start = False
        self._refresh_dur()
        # 시작 핸들 드래그 시 영상 재생 시점 동기화
        if self._yt_loaded:
            self.web.page().runJavaScript(f"seekTo({v});")

    def _on_slider_end(self, v: int):
        if self._upd_end: return
        self._upd_end = True
        self.end_in.setText(secs_to_hms(v))
        self._upd_end = False
        self._refresh_dur()

    def _on_start_text(self, text: str):
        if self._upd_start: return
        self._upd_start = True
        self.rslider.setStart(hms_to_secs(text))
        self._upd_start = False
        self._refresh_dur()

    def _on_end_text(self, text: str):
        if self._upd_end: return
        self._upd_end = True
        self.rslider.setEnd(hms_to_secs(text))
        self._upd_end = False
        self._refresh_dur()

    def _refresh_dur(self):
        s = hms_to_secs(self.start_in.text())
        e = hms_to_secs(self.end_in.text())
        self.dur_lbl.setText(f"구간: {secs_to_hms(max(0, e - s))}")
        self._update_size_estimate()

    # 해상도별 대략적 bitrate (bytes/sec) — YouTube 평균 기준
    # 실제 비트레이트는 영상 콘텐츠에 따라 크게 달라질 수 있음
    _RES_BPS = {
        '최고화질': 3_500_000,  # ~28 Mbps (4K)
        '1080p':    1_000_000,  # ~8 Mbps
        '720p':       500_000,  # ~4 Mbps
        '480p':       200_000,  # ~1.6 Mbps
        '360p':       100_000,  # ~0.8 Mbps
    }
    _GIF_BPS = {
        '320': 400_000, '480': 1_000_000, '640': 2_200_000,
        '960': 5_000_000, '1280': 9_000_000, '원본': 25_000_000,
    }

    def _calc_size_bytes(self, clip_secs: float, ext: str, res: str,
                          gif_width: str, mute: bool,
                          local_file: str = '', duration: float = 0) -> int:
        """주어진 파라미터로 예상 파일 크기(bytes)를 계산."""
        if ext == 'mp3 (오디오만)':
            bps = 16_000
        elif ext == 'gif':
            bps = self._GIF_BPS.get(gif_width, 1_000_000)
        else:
            if (local_file and os.path.isfile(local_file)
                    and duration > 1 and res == '최고화질'):
                try:
                    bps = os.path.getsize(local_file) / duration
                except OSError:
                    bps = self._RES_BPS.get(res, 5_000_000)
            else:
                bps = self._RES_BPS.get(res, 5_000_000)
            if mute:
                bps = max(0, bps - 16_000)
        return int(bps * max(1, clip_secs))

    def _update_size_estimate(self):
        clip_secs = max(1, self.rslider.end - self.rslider.start)
        ext = self.ext_combo.currentText()
        mute = self.mute_chk.isChecked()
        res_sel = self.res_combo.currentText()
        local_f = self._local_file if self._mode == 'local' else ''
        est = self._calc_size_bytes(clip_secs, ext, res_sel,
                                    self.gif_width.currentText(), mute,
                                    local_f, self._duration)
        note = " (참고용, VBR 영상은 실제와 다를 수 있음)" if (
            local_f and ext not in ('mp3 (오디오만)', 'gif') and res_sel == '최고화질'
        ) else " (참고용)"
        if est:
            self.size_est_lbl.setText(f"  예상 크기: ~{self._size_str(est)}{note}")
        else:
            self.size_est_lbl.setText("")

    # -----------------------------------------------------------------------
    # Set start/end from current player time
    # -----------------------------------------------------------------------
    def _set_start_from_cur(self):
        if self._yt_loaded:
            self.web.page().runJavaScript("getCurrentTime();", self._apply_start)

    def _apply_start(self, val):
        if val is not None:
            self._upd_start = True
            s = int(val)
            self.start_in.setText(secs_to_hms(s))
            self.rslider.setStart(s)
            self._upd_start = False
            self._refresh_dur()

    def _set_end_from_cur(self):
        if self._yt_loaded:
            self.web.page().runJavaScript("getCurrentTime();", self._apply_end)

    def _apply_end(self, val):
        if val is not None:
            self._upd_end = True
            s = int(val)
            self.end_in.setText(secs_to_hms(s))
            self.rslider.setEnd(s)
            self._upd_end = False
            self._refresh_dur()

    # -----------------------------------------------------------------------
    # Overlay (click-to-load)
    # -----------------------------------------------------------------------
    def _update_overlay(self, text: str = ''):
        """URL 입력 내용 변화에 따라 오버레이 표시/숨김."""
        if self._yt_loaded:
            self._web_container.hide_overlay()
            return
        if self._mode == 'url':
            if self.url_input.currentText().strip():
                self._web_container.show_overlay("▶  클릭하여 로드")
            else:
                self._web_container.hide_overlay()
        else:
            if not self._local_file:
                self._web_container.show_overlay("📂  클릭하여 파일 선택")
            else:
                self._web_container.hide_overlay()

    def _on_overlay_clicked(self):
        if self._mode == 'url':
            self.load_video()
        else:
            self._browse_local_file()

    # -----------------------------------------------------------------------
    # Seek
    # -----------------------------------------------------------------------
    def _seek_start(self):
        if self._yt_loaded:
            self.web.page().runJavaScript(f"seekTo({self.rslider.start});")

    def _seek_start_and_play(self):
        """시작 구간으로 seek 후 재생."""
        if not self._yt_loaded:
            return
        s = self.rslider.start
        self.web.page().runJavaScript(
            f"seekTo({s});"
            f"(function(){{"
            f"if(typeof player!=='undefined'&&player&&player.playVideo){{player.playVideo();}}"
            f"else{{var v=document.getElementById('v');if(v)v.play().catch(function(){{}});}}"
            f"}})();"
        )

    def _seek_end(self):
        """종료 핸들 릴리즈: 재생 위치가 종료점 이후면 종료점으로 클램프."""
        if self._yt_loaded:
            v = self.rslider.end
            self.web.page().runJavaScript(
                f"(function(){{var t=getCurrentTime();if(t>{v})seekTo({v});}})();")

    def _seek_end_confirm(self):
        """종료 ▶ 확인 버튼: 입력한 종료 시간으로 무조건 이동."""
        if self._yt_loaded:
            self.web.page().runJavaScript(f"seekTo({self.rslider.end});")

    # -----------------------------------------------------------------------
    # Output filename helper
    # -----------------------------------------------------------------------
    def _unique_output(self, save_dir: str, actual_ext: str) -> str:
        """yt-dlp용 -o 인수 반환. 커스텀 이름이 있으면 중복 방지 처리."""
        custom = re.sub(r'[\\/:*?"<>|]', '_', self.name_input.text().strip())
        if custom:
            path = unique_path(os.path.join(save_dir, f"{custom}.{actual_ext}"))
            return os.path.basename(path).rsplit('.', 1)[0] + '.%(ext)s'
        return '%(title).80B_clip.%(ext)s'

    def _get_output_name(self, actual_ext: str, fallback_stem: str = '%(title).80B_clip') -> str:
        """입력된 파일명이 있으면 사용, 없으면 fallback_stem 사용. 확장자 제외."""
        custom = self.name_input.text().strip()
        # Windows 파일명 금지 문자 제거
        custom = re.sub(r'[\\/:*?"<>|]', '_', custom) if custom else ''
        return custom if custom else fallback_stem

    # -----------------------------------------------------------------------
    # Save path
    # -----------------------------------------------------------------------
    def _browse_folder(self):
        current = self.path_input.currentText().strip() or DOWNLOAD_DIR
        folder = QFileDialog.getExistingDirectory(self, "저장 폴더 선택", current)
        if folder:
            self._add_recent_path(folder)
            self._refresh_file_list()

    def _add_recent_path(self, path: str):
        """경로를 최근 목록 맨 앞에 추가 (최대 5개, 중복 제거)."""
        path = os.path.normpath(path)
        # 기존 항목에서 동일 경로 제거
        for i in range(self.path_input.count() - 1, -1, -1):
            if os.path.normpath(self.path_input.itemText(i)) == path:
                self.path_input.removeItem(i)
        self.path_input.insertItem(0, path)
        # 최대 5개 유지
        while self.path_input.count() > 5:
            self.path_input.removeItem(self.path_input.count() - 1)
        self.path_input.setCurrentIndex(0)

    def _add_recent_url(self, url: str):
        """URL을 최근 목록 맨 앞에 추가 (최대 8개, 중복 제거). userData=URL, text=제목(추후 업데이트)."""
        for i in range(self.url_input.count() - 1, -1, -1):
            if self.url_input.itemData(i) == url or self.url_input.itemText(i) == url:
                self.url_input.removeItem(i)
        self.url_input.insertItem(0, url, url)   # text=URL (제목 오면 덮어씀), data=URL
        while self.url_input.count() > 8:
            self.url_input.removeItem(self.url_input.count() - 1)
        self.url_input.setCurrentIndex(0)

    def _update_recent_url_title(self, url: str, title: str):
        """히스토리 항목의 표시 텍스트를 제목으로 업데이트."""
        if not title:
            return
        for i in range(self.url_input.count()):
            if self.url_input.itemData(i) == url:
                self.url_input.setItemText(i, f"{title}  [{url[:40]}{'…' if len(url)>40 else ''}]")
                # 현재 선택 항목이면 lineEdit도 URL로 유지 (제목이 입력창에 들어가지 않게)
                if self.url_input.currentIndex() == i:
                    self.url_input.lineEdit().setText(url)
                break

    def _open_folder(self):
        import subprocess as _sp
        folder = self.path_input.currentText().strip() or DOWNLOAD_DIR
        os.makedirs(folder, exist_ok=True)
        _sp.Popen(['explorer', os.path.normpath(folder)])

    # -----------------------------------------------------------------------
    # Format combo
    # -----------------------------------------------------------------------
    def _on_ext_changed(self, text: str):
        is_mp3 = text == 'mp3 (오디오만)'
        is_gif  = text == 'gif'
        self.res_combo.setEnabled(not is_mp3 and not is_gif)
        self.gif_row.setVisible(is_gif)
        # mp3/gif는 소리 제거 옵션 불필요
        self.mute_chk.setEnabled(not is_mp3 and not is_gif)
        if is_mp3 or is_gif:
            self.mute_chk.setChecked(False)
        if is_mp3:
            disp = ".mp3"
        elif is_gif:
            disp = ".gif"
        else:
            disp = f".{text}"
        self.ext_lbl.setText(disp)
        self.name_ext_lbl.setText(disp)
        self._update_size_estimate()

    def _on_gif_width_changed(self, text: str):
        if text == '원본':
            self._gif_warn_lbl.setText("⚠ 원본 크기는 파일이 매우 클 수 있습니다. 재생 속도가 느려질 수 있습니다.")
            self._gif_warn_lbl.setStyleSheet("color: #e67e22;")
        else:
            self._gif_warn_lbl.setText("※ GIF는 파일 크기가 클 수 있습니다.")
            self._gif_warn_lbl.setStyleSheet("")
        self._update_size_estimate()

    def _on_speed_changed(self, text: str):
        try:
            self._speed = float(text.replace('x', ''))
        except ValueError:
            self._speed = 1.0
        if self._yt_loaded:
            self.web.page().runJavaScript(f"setRate({self._speed});")

    def _build_speed_filters(self, speed: float):
        """속도 변환 필터 튜플 (video_filter, audio_filter) 반환. speed==1이면 (None, None)."""
        if abs(speed - 1.0) < 0.001:
            return None, None
        vf = f"setpts={1.0 / speed:.6f}*PTS"
        af = _build_atempo(speed)
        return vf, af

    def _build_video_filters(self):
        """배속 + 해상도 스케일 + 회전/반전을 합친 vf 문자열 반환."""
        parts = []
        if abs(self._speed - 1.0) > 0.001:
            parts.append(f"setpts={1.0 / self._speed:.6f}*PTS")
        _RES_H = {'1080p': 1080, '720p': 720, '480p': 480, '360p': 360}
        scale_h = _RES_H.get(self.res_combo.currentText())
        if scale_h:
            parts.append(f'scale=-2:{scale_h}')
        _ROT = {
            '90° 시계':   'transpose=1',
            '90° 반시계': 'transpose=2',
            '180°':       'transpose=2,transpose=2',
            '좌우 반전':  'hflip',
            '상하 반전':  'vflip',
        }
        rot = _ROT.get(self.rot_combo.currentText())
        if rot:
            parts.append(rot)
        return ','.join(parts) if parts else None

    def _build_audio_filters(self):
        """배속 + 볼륨을 합친 af 문자열 반환."""
        parts = []
        if abs(self._speed - 1.0) > 0.001:
            parts.append(_build_atempo(self._speed))
        vol = self.vol_spin.value()
        if abs(vol - 1.0) > 0.01:
            parts.append(f'volume={vol:.2f}')
        return ','.join(parts) if parts else None

    def _needs_post_encode(self):
        """ffmpeg 후처리 재인코딩이 필요한지 여부."""
        return (
            self.rot_combo.currentText() != '없음'
            or abs(self.vol_spin.value() - 1.0) > 0.01
            or self.crf_spin.value() > 0
            or abs(self._speed - 1.0) > 0.001
        )

    def _cookie_args(self):
        """쿠키 파일이 설정된 경우 yt-dlp --cookies 인수 반환."""
        if self._cookie_file and os.path.exists(self._cookie_file):
            return ['--cookies', self._cookie_file]
        return []

    # -----------------------------------------------------------------------
    # Download
    # -----------------------------------------------------------------------
    def start_download(self):
        if self._queue_running or self._gif_queue_running:
            self._log("큐 실행 중에는 개별 다운로드를 시작할 수 없습니다.")
            return
        if self._mode == 'local':
            url = self._local_file or ''
        else:
            url = (self.url_input.currentData() or self.url_input.currentText()).strip()
        if not url:
            self._log("URL 또는 로컬 파일을 먼저 선택하세요.")
            return

        import time as _time
        self._dl_start_time   = _time.time()
        self._last_saved_path = None

        start_s = self.rslider.start
        end_s   = self.rslider.end
        if end_s <= start_s:
            self._log("오류: 종료 시간이 시작 시간보다 커야 합니다.")
            return

        self._right_tabs.setCurrentIndex(1)  # 로그 탭으로 전환
        start_str = secs_to_hms(start_s)
        end_str   = secs_to_hms(end_s)
        res = self.res_combo.currentText()
        ext = self.ext_combo.currentText()
        mute = self.mute_chk.isChecked()
        fmt_args, actual_ext = build_format_args(res, ext, mute=mute)
        self.ext_lbl.setText(f".{actual_ext}")

        save_dir = self.path_input.currentText().strip() or DOWNLOAD_DIR
        os.makedirs(save_dir, exist_ok=True)

        args = (
            ['--download-sections', f'*{start_str}-{end_str}']
            + fmt_args
            + self._cookie_args()
            + ['--ffmpeg-location', FFMPEG_DIR,
               '-P', save_dir,
               '-o', self._unique_output(save_dir, actual_ext),
               '--windows-filenames',
               '--no-part',
               url]
        )

        self._log(f"▶  [{start_str} ~ {end_str}]  해상도:{res}  형식:.{actual_ext}")
        self._log(f"   저장 위치: {save_dir}")

        self.dl_btn.setEnabled(False)
        self.dl_btn.setText("다운로드 중...")
        self.cancel_btn.setEnabled(True)
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        # 구간 길이(초) 저장 — ffmpeg 진행률 계산에 사용
        self._clip_secs = max(1, end_s - start_s)

        if self._mode == 'local':
            self._start_local_clip(start_str, end_str, actual_ext, save_dir)
        elif actual_ext == 'gif':
            self._start_gif_stage1(url, start_str, end_str, save_dir)
        elif self._needs_post_encode():
            # 효과 적용 → 임시 다운로드 후 ffmpeg 후처리 (2단계)
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = self._get_output_name(actual_ext, f'clip_{ts}')
            self._speed_save_dir  = save_dir
            self._speed_actual_ext = actual_ext
            self._speed_final_out = unique_path(os.path.join(save_dir, f"{stem}.{actual_ext}"))
            self._speed_temp      = None  # 다운로드 후 glob으로 탐색
            temp_args = (
                ['--download-sections', f'*{start_str}-{end_str}']
                + fmt_args
                + self._cookie_args()
                + ['--ffmpeg-location', FFMPEG_DIR,
                   '-P', save_dir,
                   '-o', f'_clipdl_speed_temp_{os.getpid()}.%(ext)s',
                   '--windows-filenames',
                   '--no-part',
                   url]
            )
            self._log(f"▶ 효과 적용 — 1/2  임시 다운로드 중...")
            self._run_process(YTDLP_EXE, temp_args, self._on_url_speed_dl_done)
        else:
            self._run_process(YTDLP_EXE, args, self._on_done)

    # -----------------------------------------------------------------------
    # URL 속도 변환 (2단계)
    # -----------------------------------------------------------------------
    def _on_url_speed_dl_done(self, code: int, _status):
        if code != 0:
            self._log(f"✘ 다운로드 실패 (코드: {code})")
            self._cleanup_speed_temp()
            self._reset_dl_ui()
            return
        import glob as _glob
        matches = _glob.glob(
            os.path.join(self._speed_save_dir, f'_clipdl_speed_temp_{os.getpid()}.*'))
        if not matches:
            self._log("✘ 임시 파일을 찾을 수 없습니다.")
            self._reset_dl_ui()
            return
        self._speed_temp = matches[0]
        vf = self._build_video_filters()
        af = self._build_audio_filters()
        mute = self.mute_chk.isChecked()
        crf  = self.crf_spin.value()
        ffmpeg_args = ['-y', '-i', self._speed_temp]
        if vf:
            ffmpeg_args += ['-filter:v', vf]
        if mute:
            ffmpeg_args += ['-an']
        elif af:
            ffmpeg_args += ['-filter:a', af]
        if crf > 0:
            ffmpeg_args += ['-crf', str(crf)]
        ffmpeg_args += [self._speed_final_out]
        self._log(f"▶ 효과 적용 — 2/2  인코딩 중...")
        self.progress.setRange(0, 0)
        self._run_process(FFMPEG_EXE, ffmpeg_args, self._on_url_speed_encode_done)

    def _on_url_speed_encode_done(self, code: int, _status):
        self._cleanup_speed_temp()
        path = getattr(self, '_speed_final_out', '')
        if code == 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self._new_file_paths.add(path)
            self._log_link(f"✔ 완료!  저장 위치: ", path)
            self._refresh_file_list()
            if self.autoopen_chk.isChecked():
                os.startfile(path)
        else:
            self._log(f"✘ 속도 변환 실패 (코드: {code})")
        if self._queue_running:
            self._on_queue_item_done(path if code == 0 else '', code == 0)
        else:
            self._reset_dl_ui()

    def _cleanup_speed_temp(self):
        tmp = getattr(self, '_speed_temp', None)
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # 스크린샷
    # -----------------------------------------------------------------------
    def _screenshot(self):
        cur_text = self.cur_lbl.text()   # "현재 위치:  HH:MM:SS"
        parts = cur_text.split()
        time_str = parts[-1] if parts and ':' in parts[-1] else '00:00:00'
        save_dir = self.path_input.currentText().strip() or DOWNLOAD_DIR
        os.makedirs(save_dir, exist_ok=True)
        from datetime import datetime
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = unique_path(os.path.join(save_dir, f'screenshot_{ts}.png'))
        if self._mode == 'local' and self._local_file and os.path.exists(self._local_file):
            args = ['-y', '-ss', time_str, '-i', self._local_file, '-frames:v', '1', out]
            self._run_process(FFMPEG_EXE, args,
                              lambda c, s: self._on_screenshot_done(c, s, out))
        else:
            pixmap = self.web.grab()
            pixmap.save(out, 'PNG')
            self._on_screenshot_done(0, '', out)

    def _on_screenshot_done(self, code: int, _status, path: str):
        if code == 0:
            self._log_link("📷 스크린샷 저장: ", path)
            self._refresh_file_list()
        else:
            self._log("✘ 스크린샷 저장 실패")

    # -----------------------------------------------------------------------
    # 쿠키 파일 설정
    # -----------------------------------------------------------------------
    def _set_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "쿠키 파일 선택", "",
            "쿠키 파일 (*.txt);;모든 파일 (*.*)")
        if path:
            self._cookie_file = path
            self._log(f"🍪 쿠키 파일 설정: {path}")
        else:
            self._cookie_file = None
            self._log("🍪 쿠키 파일 설정 해제")

    # -----------------------------------------------------------------------
    # Local file clip  (ffmpeg 직접 사용)
    # -----------------------------------------------------------------------
    def _start_local_clip(self, start_str: str, end_str: str, ext: str, save_dir: str):
        if not self._local_file or not os.path.exists(self._local_file):
            self._log("오류: 로컬 파일이 선택되지 않았습니다.")
            self._reset_dl_ui()
            return

        from datetime import datetime
        base = os.path.splitext(os.path.basename(self._local_file))[0]
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")

        if ext == 'gif':
            # 로컬 파일은 중간 인코딩 없이 바로 GIF 변환
            stem = self._get_output_name('gif', f'{base}_clip_{ts}')
            gif  = unique_path(os.path.join(save_dir, f"{stem}.gif"))
            fps = self.gif_fps.currentText()
            w   = self.gif_width.currentText()
            scale = f"scale={w}:-2:flags=lanczos," if w != '원본' else ""
            pts_f = (f"setpts={1.0 / self._speed:.6f}*PTS,"
                     if abs(self._speed - 1.0) > 0.001 else "")
            vf = (f"{pts_f}fps={fps},{scale}split[s0][s1];"
                  f"[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
            dur = hms_to_secs(end_str) - hms_to_secs(start_str)
            # GIF 입력 시 probesize 확대 (프레임 수 추정 경고 방지)
            probe = (['-probesize', '100M', '-analyzeduration', '100M']
                     if self._local_file.lower().endswith('.gif') else [])
            args = (['-y'] + probe
                    + ['-ss', start_str, '-t', str(dur),
                       '-i', self._local_file,
                       '-vf', vf, '-r', fps, '-loop', '0', gif])
            self._log("GIF 변환 중...")
            self._run_process(FFMPEG_EXE, args,
                              lambda c, s: self._on_gif_stage2_done(c, s, gif))
        else:
            mute = self.mute_chk.isChecked()
            stem = self._get_output_name(ext, f'{base}_clip_{ts}')
            out  = unique_path(os.path.join(save_dir, f"{stem}.{ext}"))
            vf  = self._build_video_filters()
            af  = self._build_audio_filters()
            crf = self.crf_spin.value()
            if ext in ('mp3',):
                base_args = ['-y', '-i', self._local_file,
                             '-ss', start_str, '-to', end_str, '-vn']
                if af:
                    base_args += ['-filter:a', af]
                args = base_args + ['-acodec', 'mp3', out]
            else:
                if vf or af or crf > 0:
                    # 재인코딩 (효과 적용)
                    base_args = ['-y', '-i', self._local_file,
                                 '-ss', start_str, '-to', end_str]
                    if vf:
                        base_args += ['-filter:v', vf]
                    if mute:
                        base_args += ['-an']
                    elif af:
                        base_args += ['-filter:a', af]
                    if crf > 0:
                        base_args += ['-crf', str(crf)]
                    args = base_args + [out]
                else:
                    # input seeking(-ss before -i): 키프레임에서 시작 → 첫 프레임 흰 화면 방지
                    # -t duration 사용 (input seeking 시 -to는 입력 기준 절대값이므로 부정확)
                    dur_s = hms_to_secs(end_str) - hms_to_secs(start_str)
                    if mute:
                        args = ['-y', '-ss', start_str, '-i', self._local_file,
                                '-t', str(dur_s), '-c:v', 'copy', '-an', out]
                    elif os.path.splitext(out)[1].lower() in ('.mp4', '.m4v'):
                        # MP4/M4V는 Opus 미지원 → 오디오 AAC 재인코딩
                        args = ['-y', '-ss', start_str, '-i', self._local_file,
                                '-t', str(dur_s), '-c:v', 'copy', '-c:a', 'aac', out]
                    else:
                        args = ['-y', '-ss', start_str, '-i', self._local_file,
                                '-t', str(dur_s), '-c', 'copy', out]
            self._last_saved_path = out
            self._log(f"ffmpeg 구간 추출: {start_str} ~ {end_str}")
            self._run_process(FFMPEG_EXE, args, self._on_done)

    # -----------------------------------------------------------------------
    # GIF  (2단계: yt-dlp 다운로드 → ffmpeg 변환)
    # -----------------------------------------------------------------------
    def _start_gif_stage1(self, url, start_str, end_str, save_dir):
        """1단계: yt-dlp로 임시 mp4 다운로드."""
        self._gif_temp = os.path.join(save_dir, f'_clipdl_temp_{os.getpid()}.mp4')
        self._gif_save_dir = save_dir
        if os.path.exists(self._gif_temp):
            os.remove(self._gif_temp)

        w = self.gif_width.currentText()
        fmt = f'bv[width<={w}]/best[width<={w}]/bv*/best' if w != '원본' else 'bv*/best'
        args = (
            ['--download-sections', f'*{start_str}-{end_str}',
             '-f', fmt,
             '--merge-output-format', 'mp4',
             '--ffmpeg-location', FFMPEG_DIR,
             '-P', save_dir,
             '-o', f'_clipdl_temp_{os.getpid()}.mp4',
             '--no-part',
             '--windows-filenames']
            + self._cookie_args()
            + [url]
        )
        self._log("1/2  영상 다운로드 중...")
        self._run_process(YTDLP_EXE, args, self._on_gif_stage1_done)

    def _on_gif_stage1_done(self, code, _status):
        if code != 0:
            self._log(f"✘ 영상 다운로드 실패 (코드: {code})")
            self._cleanup_gif_temp()
            if self._gif_queue_running:
                self._on_gif_queue_item_done('', False)
            else:
                self._reset_dl_ui()
            return
        if not os.path.exists(self._gif_temp):
            self._log("✘ 임시 파일을 찾을 수 없습니다.")
            if self._gif_queue_running:
                self._on_gif_queue_item_done('', False)
            else:
                self._reset_dl_ui()
            return

        # 2단계: ffmpeg로 GIF 변환
        from datetime import datetime
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = self._get_output_name('gif', f'clip_{ts}')
        gif  = unique_path(os.path.join(self._gif_save_dir, f"{stem}.gif"))
        fps = self.gif_fps.currentText()
        w   = self.gif_width.currentText()
        scale = f"scale={w}:-2:flags=lanczos," if w != '원본' else ""
        pts_f = (f"setpts={1.0 / self._speed:.6f}*PTS,"
                 if abs(self._speed - 1.0) > 0.001 else "")
        vf = (f"{pts_f}fps={fps},{scale}split[s0][s1];"
              f"[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
        args = ['-y', '-i', self._gif_temp, '-vf', vf, '-r', fps, '-loop', '0', gif]

        self._log("2/2  GIF 변환 중...")
        self.progress.setRange(0, 0)
        self._run_process(FFMPEG_EXE, args,
                          lambda c, s: self._on_gif_stage2_done(c, s, gif))

    def _cleanup_gif_temp(self):
        tmp = getattr(self, '_gif_temp', None)
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def _on_gif_stage2_done(self, code, _status, gif_path):
        self._cleanup_gif_temp()
        if code == 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self._new_file_paths.add(gif_path)
            self._log_link("✔ GIF 완료!  저장 위치: ", gif_path)
            self._refresh_file_list()
            if self.autoopen_chk.isChecked():
                os.startfile(gif_path)
        else:
            self._log(f"✘ GIF 변환 실패 (코드: {code})")
        if self._queue_running:
            self._on_queue_item_done(gif_path if code == 0 else '', code == 0)
        elif self._gif_queue_running:
            self._on_gif_queue_item_done(gif_path if code == 0 else '', code == 0)
        else:
            self._reset_dl_ui()

    def _run_process(self, exe: str, args: list, on_finish):
        """공통 QProcess 실행 헬퍼."""
        self._process = QProcess(self)
        self._process.setWorkingDirectory(YTDLP_DIR)
        self._process.setProcessChannelMode(QProcess.MergedChannels)
        self._process.readyRead.connect(self._on_output)
        self._process.finished.connect(on_finish)
        self._process.start(exe, args)

    def cancel_download(self):
        if self._process and self._process.state() != QProcess.NotRunning:
            self._process.kill()
            self._log("⚠ 다운로드 취소됨.")
        self._cleanup_gif_temp()
        self._cleanup_gif_queue_temp()
        self._cleanup_speed_temp()
        # 큐 실행 중에는 _reset_dl_ui 호출 안 함 — 큐 콜백이 UI 상태를 관리
        if not self._queue_running and not self._gif_queue_running:
            self._reset_dl_ui()

    def _on_output(self):
        raw  = self._process.readAll().data()
        text = raw.decode('utf-8', errors='replace')
        # ANSI 이스케이프 코드 제거 (yt-dlp 컬러 출력)
        text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
        # yt-dlp는 \r로 같은 줄을 덮어쓰므로 \r\n 모두 구분자로 사용
        for line in re.split(r'[\r\n]+', text):
            line = line.strip()
            if not line:
                continue
            self._log(line)

            # yt-dlp: [download]  45.3% of ~8.50MiB at 2.1MiB/s ETA 00:05
            m = re.search(r'\[download\]\s+([\d.]+)%', line)
            if m:
                self.progress.setRange(0, 100)
                self.progress.setValue(int(float(m.group(1))))
                continue

            # ffmpeg: time=HH:MM:SS.ss → 구간 길이 대비 퍼센트 계산
            m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
            if m:
                elapsed = int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
                pct = min(100, int(elapsed / self._clip_secs * 100))
                self.progress.setRange(0, 100)
                self.progress.setValue(pct)

    def _on_done(self, code: int, _status):
        if code == 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            save_dir = self.path_input.currentText().strip() or DOWNLOAD_DIR
            self._add_recent_path(save_dir)
            path = self._last_saved_path or self._find_new_file(save_dir)
            if path and os.path.isfile(path):
                self._new_file_paths.add(path)
                self._log_link("✔ 완료!  저장 위치: ", path)
                if self.autoopen_chk.isChecked():
                    os.startfile(path)
            else:
                self._log(f"✔ 완료!  저장 위치: {save_dir}")
            self._refresh_file_list()
        else:
            self._log(f"✘ 실패 (종료 코드: {code})")
        if self._queue_running:
            self._on_queue_item_done(
                self._last_saved_path or self._find_new_file(
                    self.path_input.currentText().strip() or DOWNLOAD_DIR) or '', code == 0)
        else:
            self._reset_dl_ui()

    def _apply_local_gif_mode(self, path: str):
        """GIF 파일 로드 시 UI 자동 조정, 일반 파일 시 복원."""
        is_gif = path.lower().endswith('.gif')
        prev_gif = self._local_is_gif
        self._local_is_gif = is_gif
        if is_gif:
            self.ext_combo.setCurrentText('gif')
            self.ext_combo.setEnabled(False)
            self.dl_btn.setText("구간 자르기")
            if not self._queue_running and not self._gif_queue_running:
                self.queue_add_btn.setEnabled(True)
            self._probe_gif_properties(path)
        else:
            if prev_gif:
                # GIF → 영상으로 바뀔 때 ext 자동 mp4 복원
                self.ext_combo.setEnabled(True)
                self.ext_combo.setCurrentText('mp4')
            else:
                self.ext_combo.setEnabled(True)
            # gif_fps / gif_width 다시 활성화
            self.gif_fps.setEnabled(True)
            self.gif_width.setEnabled(True)
            if not self._queue_running:
                self.dl_btn.setText("구간 저장 (다운로드)")
                self.queue_add_btn.setEnabled(True)

    def _probe_gif_properties(self, path: str):
        """ffprobe로 GIF의 fps·폭을 읽어 gif_fps/gif_width 콤보에 반영 후 비활성화."""
        ffprobe = os.path.join(FFMPEG_DIR, 'ffprobe.exe')
        exe = ffprobe if os.path.isfile(ffprobe) else FFMPEG_EXE
        args = ['-v', 'quiet', '-select_streams', 'v:0',
                '-show_entries', 'stream=width,r_frame_rate,nb_frames',
                '-of', 'csv=p=0', path]
        if exe == FFMPEG_EXE:
            args = ['-i', path]  # ffmpeg fallback
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        use_ffprobe = (exe == ffprobe)
        proc.finished.connect(lambda *_, p=proc, fp=use_ffprobe: self._on_gif_props_done(p, fp))
        proc.start(exe, args)
        self._gif_props_proc = proc

    def _on_gif_props_done(self, _proc: QProcess, is_ffprobe: bool):
        out = _proc.readAll().data().decode('utf-8', errors='replace').strip()
        width, fps_int, nb_frames = None, None, None
        if is_ffprobe:
            # 예: "480,10/1,100"  (width, r_frame_rate, nb_frames)
            for line in out.splitlines():
                parts = line.strip().split(',')
                if len(parts) >= 2:
                    try:
                        width = int(parts[0])
                        num, den = (parts[1].split('/') + ['1'])[:2]
                        fps_int = max(1, round(int(num) / max(1, int(den))))
                        if len(parts) >= 3 and parts[2].isdigit():
                            nb_frames = int(parts[2])
                    except (ValueError, ZeroDivisionError):
                        pass
                    break
        else:
            m = re.search(r'(\d+)x(\d+)', out)
            if m:
                width = int(m.group(1))
            m2 = re.search(r'(\d+(?:\.\d+)?)\s*fps', out)
            if m2:
                fps_int = max(1, round(float(m2.group(1))))

        # 슬라이더 범위: nb_frames / fps 로 duration 계산
        if fps_int and nb_frames:
            dur = max(1, round(nb_frames / fps_int))
            self._duration = dur
            self._reset_slider(dur)
            self._update_size_estimate()
        elif fps_int is None or nb_frames is None:
            # fallback: ffprobe format duration 시도
            self._probe_local_duration()

        if fps_int is not None:
            closest_fps = min(GIF_FPS_OPTIONS, key=lambda x: abs(int(x) - fps_int))
            self.gif_fps.setCurrentText(closest_fps)
        if width is not None:
            w_opts = [int(x) for x in GIF_W_OPTIONS if x != '원본']
            if width > max(w_opts):
                self.gif_width.setCurrentText('원본')
            else:
                closest_w = str(min(w_opts, key=lambda x: abs(x - width)))
                self.gif_width.setCurrentText(closest_w)
        self.gif_fps.setEnabled(False)
        self.gif_width.setEnabled(False)

    def _reset_dl_ui(self):
        running     = self._queue_running
        gif_running = self._gif_queue_running
        any_running = running or gif_running
        is_gif_local = self._local_is_gif and self._mode == 'local'
        self.dl_btn.setEnabled(not any_running)
        self.dl_btn.setText("구간 자르기" if is_gif_local else "구간 저장 (다운로드)")
        self.queue_add_btn.setEnabled(not any_running)
        self._queue_run_btn.setEnabled(not running)
        self._queue_clear_btn.setEnabled(not running)
        self._gif_queue_run_btn.setEnabled(not gif_running)
        self._gif_queue_clear_btn.setEnabled(not gif_running)
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")

    # -----------------------------------------------------------------------
    # File tree (right panel)
    # -----------------------------------------------------------------------
    def _badge_icon(self, base_icon):
        """시스템 아이콘 우측 하단에 파란 점 뱃지를 합성해 반환."""
        from PyQt5.QtGui import QPixmap, QIcon as _QIcon
        pm = base_icon.pixmap(16, 16).copy()
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#4A90D9'))
        p.drawEllipse(9, 9, 6, 6)   # 우하단 파란 점
        p.end()
        return _QIcon(pm)

    @staticmethod
    def _size_str(sz: int) -> str:
        if sz >= 1024 * 1024:
            return f"{sz / 1024 / 1024:.1f} MB"
        if sz >= 1024:
            return f"{sz / 1024:.0f} KB"
        return f"{sz} B"

    def _update_folder_path_lbl(self):
        folder = self.path_input.currentText().strip() or DOWNLOAD_DIR
        self._folder_path_lbl.setText(folder)

    def _open_save_folder(self):
        folder = self.path_input.currentText().strip() or DOWNLOAD_DIR
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def _refresh_file_list(self):
        folder = self.path_input.currentText().strip() or DOWNLOAD_DIR
        self._folder_lbl.setText(f"저장 폴더  ({os.path.basename(folder) or folder})")
        self._folder_path_lbl.setText(folder)
        self.file_tree.blockSignals(True)
        self.file_tree.clear()
        self.file_tree.blockSignals(False)
        self._audio_pending.clear()
        self._path_to_tree_item.clear()
        if not os.path.isdir(folder):
            return
        self._populate_tree(self.file_tree.invisibleRootItem(), folder)
        self._start_audio_checks()

    def _populate_tree(self, parent, folder: str):
        from datetime import datetime as _dt
        try:
            raw = list(os.scandir(folder))
        except PermissionError:
            return

        def _sort_key(e):
            if self._sort_col == 1:   # 확장자
                return os.path.splitext(e.name)[1].lower()
            elif self._sort_col == 2:  # 크기
                try:    return e.stat().st_size if e.is_file() else 0
                except: return 0
            elif self._sort_col == 3:  # 날짜
                try:    return e.stat().st_mtime
                except: return 0.0
            return e.name.lower()      # 이름(0) 또는 기본

        skip = lambda e: e.name.startswith('.') or e.name.startswith('_')
        dirs  = sorted([e for e in raw if e.is_dir()  and not skip(e)],
                       key=_sort_key, reverse=not self._sort_asc)
        files = sorted([e for e in raw if e.is_file() and not skip(e)],
                       key=_sort_key, reverse=not self._sort_asc)

        self.file_tree.blockSignals(True)
        for e in dirs + files:
            item = QTreeWidgetItem(parent)
            _stem, _ext = os.path.splitext(e.name)
            # 폴더는 확장자 없이 전체 이름 표시
            item.setText(0, _stem if (e.is_file() and _ext) else e.name)
            item.setData(0, Qt.UserRole, e.path)
            item.setIcon(0, self._icon_provider.icon(QFileInfo(e.path)))
            item.setToolTip(0, e.path)
            try:
                mtime = e.stat().st_mtime
                item.setText(3, _dt.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"))
            except OSError:
                pass
            if e.is_dir():
                item.setData(0, Qt.UserRole + 1, 'dir')
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                placeholder = QTreeWidgetItem(item)
                placeholder.setText(0, '...')
                placeholder.setData(0, Qt.UserRole + 1, 'placeholder')
            else:
                item.setData(0, Qt.UserRole + 1, 'file')
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                # 확장자 컬럼 (점 제외)
                item.setText(1, _ext.lstrip('.').lower() if _ext else '')
                item.setTextAlignment(1, Qt.AlignCenter)
                try:
                    sz = e.stat().st_size
                    item.setText(2, self._size_str(sz))
                    item.setToolTip(0, f"{e.path}\n{self._size_str(sz)}")
                except OSError:
                    pass
                # 오디오 유무 아이콘 (컬럼 4)
                _extl = _ext.lower()
                if _extl in ('.mp3', '.m4a', '.aac', '.ogg', '.flac', '.wav'):
                    item.setText(4, '🔊')
                    item.setTextAlignment(4, Qt.AlignCenter)
                elif _extl == '.gif':
                    item.setText(4, '🔇')
                    item.setTextAlignment(4, Qt.AlignCenter)
                elif _extl in ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ts'):
                    self._path_to_tree_item[e.path] = item
                    if e.path in self._audio_cache:
                        item.setText(4, '🔊' if self._audio_cache[e.path] else '🔇')
                        item.setTextAlignment(4, Qt.AlignCenter)
                    else:
                        self._audio_pending.append(e.path)
                if e.path in self._new_file_paths:
                    for _c in range(4):
                        item.setBackground(_c, QColor('#FF5252'))
                        item.setForeground(_c, QColor('#FFFFFF'))
                    item.setIcon(0, self._badge_icon(
                        self._icon_provider.icon(QFileInfo(e.path))))
        self.file_tree.blockSignals(False)

    # -----------------------------------------------------------------------
    # 오디오 유무 비동기 확인 (ffprobe 큐)
    # -----------------------------------------------------------------------
    def _start_audio_checks(self):
        """pending 큐에서 하나씩 ffprobe로 오디오 스트림 확인."""
        if self._audio_check_proc is not None or not self._audio_pending:
            return
        path = self._audio_pending.pop(0)
        if path not in self._path_to_tree_item:
            # 이미 트리에서 사라진 항목 → 건너뜀
            self._start_audio_checks()
            return
        ffprobe = os.path.join(FFMPEG_DIR, 'ffprobe.exe')
        if not os.path.isfile(ffprobe):
            return
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.finished.connect(lambda code, _s, p=proc, fp=path: self._on_audio_probe_done(p, fp, code))
        proc.start(ffprobe, [
            '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_type',
            '-of', 'csv=p=0', path,
        ])
        self._audio_check_proc = proc

    def _on_audio_probe_done(self, proc: QProcess, path: str, code: int):
        self._audio_check_proc = None
        out = proc.readAll().data().decode('utf-8', errors='replace').strip()
        has_audio = (code == 0 and bool(out))
        self._audio_cache[path] = has_audio
        item = self._path_to_tree_item.get(path)
        if item is not None:
            item.setText(4, '🔊' if has_audio else '🔇')
            item.setTextAlignment(4, Qt.AlignCenter)
        # 다음 항목 확인
        self._start_audio_checks()

    def _on_tree_expand(self, item: QTreeWidgetItem):
        if item.childCount() == 1:
            ch = item.child(0)
            if ch.data(0, Qt.UserRole + 1) == 'placeholder':
                self.file_tree.blockSignals(True)
                item.removeChild(ch)
                self.file_tree.blockSignals(False)
                self._populate_tree(item, item.data(0, Qt.UserRole))
                self._start_audio_checks()

    def _on_tree_double_click(self, item: QTreeWidgetItem, _col):
        kind = item.data(0, Qt.UserRole + 1)
        path = item.data(0, Qt.UserRole)
        if kind == 'file' and path and os.path.isfile(path):
            os.startfile(path)

    def _on_tree_context(self, pos):
        item = self.file_tree.itemAt(pos)
        selected = [i for i in self.file_tree.selectedItems()
                    if i.data(0, Qt.UserRole + 1) in ('file', 'dir')]
        multi = len(selected) > 1
        menu = QMenu(self)
        if item and item.data(0, Qt.UserRole + 1) in ('file', 'dir'):
            # 가장 위에 있는 선택 항목 기준 (단일 동작)
            top_item = selected[0] if selected else item
            top_kind = top_item.data(0, Qt.UserRole + 1)
            top_path = top_item.data(0, Qt.UserRole)
            import subprocess as _sp
            if not multi:
                if top_kind == 'file':
                    menu.addAction("실행", lambda: os.startfile(top_path))
                    menu.addAction("폴더 열기", lambda p=top_path: _sp.Popen(
                        f'explorer /select,"{os.path.normpath(p)}"', shell=True))
                else:
                    menu.addAction("열기", lambda: _sp.Popen(
                        ['explorer', os.path.normpath(top_path)]))
                    menu.addAction("새 폴더 생성", lambda: self._create_folder(top_path))
                menu.addAction("이름 변경  (F2)", self._start_rename)
                menu.addSeparator()
            del_label = f"삭제  ({len(selected)}개)" if multi else "삭제"
            menu.addAction(del_label, self._delete_selected_items)
        else:
            folder = self.path_input.currentText().strip() or DOWNLOAD_DIR
            menu.addAction("새 폴더 생성", lambda: self._create_folder(folder))
            menu.addAction("새로고침", self._refresh_file_list)
        menu.exec_(self.file_tree.viewport().mapToGlobal(pos))

    def _start_rename(self):
        sel = [i for i in self.file_tree.selectedItems()
               if i.data(0, Qt.UserRole + 1) in ('file', 'dir')]
        item = sel[0] if sel else self.file_tree.currentItem()
        if not item or item.data(0, Qt.UserRole + 1) not in ('file', 'dir'):
            return
        self._rename_old_path = item.data(0, Qt.UserRole)
        self.file_tree.blockSignals(False)   # ensure signals on
        item.setFlags(item.flags() | Qt.ItemIsEditable)
        self.file_tree.editItem(item, 0)

    def _tree_delete_current(self):
        self._delete_selected_items()

    def _tree_open_current(self):
        sel = [i for i in self.file_tree.selectedItems()
               if i.data(0, Qt.UserRole + 1) in ('file', 'dir')]
        item = sel[0] if sel else self.file_tree.currentItem()
        if not item:
            return
        kind = item.data(0, Qt.UserRole + 1)
        path = item.data(0, Qt.UserRole)
        if kind == 'file' and path and os.path.isfile(path):
            os.startfile(path)
        elif kind == 'dir' and path and os.path.isdir(path):
            import subprocess as _sp
            _sp.Popen(['explorer', os.path.normpath(path)])

    def _on_tree_item_changed(self, item: QTreeWidgetItem, col: int):
        if col != 0 or self._rename_old_path is None:
            return
        old_path = self._rename_old_path
        self._rename_old_path = None
        new_name = item.text(0).strip()
        old_name = os.path.basename(old_path)
        if not new_name or new_name == old_name:
            self.file_tree.blockSignals(True)
            item.setText(0, old_name)
            self.file_tree.blockSignals(False)
            return
        new_path = os.path.join(os.path.dirname(old_path), new_name)
        try:
            os.rename(old_path, new_path)
            self.file_tree.blockSignals(True)
            item.setData(0, Qt.UserRole, new_path)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.file_tree.blockSignals(False)
        except Exception as e:
            self.file_tree.blockSignals(True)
            item.setText(0, old_name)
            self.file_tree.blockSignals(False)
            self._log(f"이름 변경 실패: {e}")

    def _delete_selected_items(self):
        targets = [i for i in self.file_tree.selectedItems()
                   if i.data(0, Qt.UserRole + 1) in ('file', 'dir')]
        if not targets:
            item = self.file_tree.currentItem()
            if item and item.data(0, Qt.UserRole + 1) in ('file', 'dir'):
                targets = [item]
        if not targets:
            return
        if len(targets) == 1:
            msg = f"'{os.path.basename(targets[0].data(0, Qt.UserRole))}'을(를) 삭제하시겠습니까?"
        else:
            names = '\n'.join(f"  • {os.path.basename(i.data(0, Qt.UserRole))}"
                              for i in targets[:10])
            if len(targets) > 10:
                names += f"\n  ... 외 {len(targets) - 10}개"
            msg = f"{len(targets)}개 항목을 삭제하시겠습니까?\n\n{names}"
        reply = QMessageBox.question(self, "삭제 확인", msg,
                                     QMessageBox.Yes | QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        import shutil
        for item in targets:
            path = item.data(0, Qt.UserRole)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                parent = item.parent() or self.file_tree.invisibleRootItem()
                parent.removeChild(item)
            except Exception as e:
                self._log(f"삭제 실패 ({os.path.basename(path)}): {e}")

    def _delete_item(self, item: QTreeWidgetItem, path: str):
        """단일 항목 삭제 (컨텍스트 메뉴 하위 호환)."""
        self.file_tree.setCurrentItem(item)
        self._delete_selected_items()

    def _create_folder(self, base_path: str):
        name, ok = QInputDialog.getText(self, "새 폴더", "폴더 이름:")
        if ok and name.strip():
            new_path = os.path.join(base_path, name.strip())
            try:
                os.makedirs(new_path, exist_ok=True)
                self._refresh_file_list()
            except Exception as e:
                self._log(f"폴더 생성 실패: {e}")

    def _on_tree_header_clicked(self, col: int):
        if col == 4:   # ♪ 컬럼 — 정렬 불필요
            return
        if col == self._sort_col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self.file_tree.header().setSortIndicator(col, order)
        self._refresh_file_list()

    def _on_tree_item_clicked(self, item: QTreeWidgetItem, _col: int):
        path = item.data(0, Qt.UserRole)
        if path and path in self._new_file_paths:
            self._new_file_paths.discard(path)
            for _c in range(4):
                item.setData(_c, Qt.BackgroundRole, None)
                item.setData(_c, Qt.ForegroundRole, None)
            item.setIcon(0, self._icon_provider.icon(QFileInfo(path)))
        kind = item.data(0, Qt.UserRole + 1)

    def _clear_new_highlights(self):
        if not self._new_file_paths:
            return
        paths = set(self._new_file_paths)
        self._new_file_paths.clear()
        def _reset(parent):
            for i in range(parent.childCount()):
                ch = parent.child(i)
                p = ch.data(0, Qt.UserRole)
                if p and p in paths:
                    for _c in range(4):
                        ch.setData(_c, Qt.BackgroundRole, None)
                        ch.setData(_c, Qt.ForegroundRole, None)
                    ch.setIcon(0, self._icon_provider.icon(QFileInfo(p)))
                _reset(ch)
        _reset(self.file_tree.invisibleRootItem())

    def _on_log_link_clicked(self, url):
        path = url.toLocalFile()
        if path and os.path.exists(path):
            os.startfile(path)

    def _log_link(self, prefix: str, path: str):
        """저장 경로를 클릭 가능한 링크로 로그에 출력."""
        href = QUrl.fromLocalFile(path).toString()
        html = (f'{prefix}'
                f'<a href="{href}" style="color:#5aabff;text-decoration:none;">'
                f'{path}</a>')
        self.log_box.append(html)
        self.log_box.verticalScrollBar().setValue(
            self.log_box.verticalScrollBar().maximum())

    def _find_new_file(self, save_dir: str):
        """다운로드 시작 이후 생성된 가장 새로운 파일을 반환."""
        cutoff = self._dl_start_time - 1.0
        try:
            files = [e.path for e in os.scandir(save_dir)
                     if e.is_file() and not e.name.startswith('_')
                     and e.stat().st_mtime >= cutoff]
            return max(files, key=os.path.getmtime) if files else None
        except Exception:
            return None

    def _on_file_dropped_to_player(self, path: str):
        if self._process and self._process.state() != QProcess.NotRunning:
            return  # 다운로드 중에는 무시
        self._local_file = path
        self._proxy_loaded = False   # 새 파일 → 프록시 상태 초기화
        self.local_path_lbl.setText(path)
        self._set_source(1)   # 로컬 파일 모드로 전환
        self._load_local_preview(path)
        self.name_input.setText(os.path.splitext(os.path.basename(path))[0])
        self._apply_local_gif_mode(path)

    # -----------------------------------------------------------------------
    # Log helper
    # -----------------------------------------------------------------------
    def _log(self, msg: str):
        if getattr(self, '_closing', False):
            return
        try:
            self.log_box.append(msg)
            sb = self.log_box.verticalScrollBar()
            sb.setValue(sb.maximum())
        except RuntimeError:
            pass

    def _on_log_context_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        menu = QMenu(self)
        copy_act  = menu.addAction("복사")
        menu.addSeparator()
        clear_act = menu.addAction("로그 지우기")
        action = menu.exec_(self.log_box.mapToGlobal(pos))
        if action == clear_act:
            self.log_box.clear()
        elif action == copy_act:
            self.log_box.copy()

    # -----------------------------------------------------------------------
    # Settings save / restore
    # -----------------------------------------------------------------------
    def _save_settings(self):
        s = QSettings("aram", "ClipDownloader")
        s.setValue("geometry",      self.saveGeometry())
        s.setValue("splitter",        self._splitter.saveState())
        s.setValue("left_splitter",   self._left_splitter.saveState())
        s.setValue("right_vsplitter", self._right_vsplitter.saveState())
        for i in range(4):
            s.setValue(f"tree_col_{i}", self.file_tree.columnWidth(i))
        s.setValue("sort_col", self._sort_col)
        s.setValue("sort_asc", self._sort_asc)
        recent = [self.path_input.itemText(i) for i in range(self.path_input.count())]
        s.setValue("recent_paths", recent)
        # [(url, display_text), ...] 형태로 저장
        recent_urls = [
            [self.url_input.itemData(i) or self.url_input.itemText(i),
             self.url_input.itemText(i)]
            for i in range(self.url_input.count())
        ]
        s.setValue("recent_urls", recent_urls)

    def _restore_settings(self):
        s = QSettings("aram", "ClipDownloader")
        geom = s.value("geometry")
        if geom:
            self.restoreGeometry(geom)
        sp = s.value("splitter")
        if sp:
            self._splitter.restoreState(sp)
        lsp = s.value("left_splitter")
        if lsp:
            self._left_splitter.restoreState(lsp)
        rvsp = s.value("right_vsplitter")
        if rvsp:
            self._right_vsplitter.restoreState(rvsp)
        for i, default in enumerate([160, 50, 65, 125]):
            w = s.value(f"tree_col_{i}", default, type=int)
            self.file_tree.setColumnWidth(i, w)
        self._sort_col = s.value("sort_col", 0, type=int)
        self._sort_asc = s.value("sort_asc", True, type=bool)
        order = Qt.AscendingOrder if self._sort_asc else Qt.DescendingOrder
        self.file_tree.header().setSortIndicator(self._sort_col, order)
        recent = s.value("recent_paths", [])
        if isinstance(recent, str):
            recent = [recent]
        if recent:
            self.path_input.clear()
            self.path_input.addItems(recent)
            self.path_input.setCurrentIndex(0)
        recent_urls = s.value("recent_urls", [])
        if isinstance(recent_urls, str):
            recent_urls = [[recent_urls, recent_urls]]
        if recent_urls:
            for entry in recent_urls:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    url, display = entry[0], entry[1]
                else:
                    url = display = str(entry)  # 구버전 호환
                self.url_input.addItem(display, url)
            self.url_input.setCurrentIndex(-1)
            self.url_input.lineEdit().clear()

    # -----------------------------------------------------------------------
    # Update tools
    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # 자동 업데이트 (GitHub blob SHA 비교 — 버전 번호 수동 관리 불필요)
    # -----------------------------------------------------------------------
    def _load_local_sha(self) -> str:
        """마지막으로 설치된 main.py의 GitHub blob SHA (로컬 캐시 파일)."""
        sha_file = os.path.join(BASE_DIR, '_installed_sha.txt')
        if os.path.isfile(sha_file):
            try:
                return open(sha_file, 'r').read().strip()
            except Exception:
                pass
        return ''

    def _save_local_sha(self, sha: str):
        sha_file = os.path.join(BASE_DIR, '_installed_sha.txt')
        try:
            with open(sha_file, 'w') as f:
                f.write(sha)
        except Exception:
            pass

    @staticmethod
    def _compute_local_sha() -> str:
        """실행 중인 main.py의 GitHub blob SHA 계산 (SHA1 of 'blob {size}\\0{content}').
        _installed_sha.txt 없이도 로컬 파일 버전을 정확히 판단할 수 있다."""
        try:
            import hashlib
            path = os.path.abspath(__file__)
            with open(path, 'rb') as f:
                data = f.read()
            header = f"blob {len(data)}\0".encode()
            return hashlib.sha1(header + data).hexdigest()
        except Exception:
            return ''

    @staticmethod
    def _fetch_remote_sha() -> tuple:
        """GitHub API로 main.py의 최신 blob SHA와 다운로드 URL 반환.
        반환: (sha, download_url, errmsg). 성공 시 errmsg=''."""
        try:
            import urllib.request, json
            req = urllib.request.Request(
                _GH_API_FILE,
                headers={'User-Agent': 'ClipDownloader-Updater'})
            with urllib.request.urlopen(req, timeout=10) as r:
                info = json.loads(r.read().decode('utf-8'))
            sha = info.get('sha', '')
            url = info.get('download_url', _SCRIPT_URL)
            if not sha:
                return '', '', 'SHA를 찾을 수 없습니다 (GitHub API 응답 이상)'
            return sha, url, ''
        except Exception as e:
            return '', '', str(e)

    def _check_update_background(self):
        """시작 시 백그라운드에서 변경 감지 (UI 블로킹 없음)."""
        def _run():
            remote_sha, _, err = self._fetch_remote_sha()
            if not remote_sha:
                return
            local_sha = self._load_local_sha()
            if not local_sha:
                # 최초 실행: 현재 파일의 실제 SHA로 초기화
                local_sha = self._compute_local_sha()
                if local_sha:
                    self._save_local_sha(local_sha)
            if local_sha and remote_sha != local_sha:
                self._log("🔔 업데이트가 있습니다. [도구 → 프로그램 업데이트 확인]")
                self.statusBar().showMessage(
                    "🔔 업데이트 가능  (도구 → 프로그램 업데이트 확인)", 30000)
        threading.Thread(target=_run, daemon=True).start()

    def _check_update_manual(self):
        """도구 메뉴에서 수동으로 업데이트 확인."""
        self._log("── 업데이트 확인 중 (GitHub 연결 중...) ──")
        def _run():
            remote_sha, download_url, err = self._fetch_remote_sha()
            if not remote_sha:
                self._log(f"✘ 업데이트 확인 실패: {err}")
                QTimer.singleShot(0, lambda: QMessageBox.warning(self, "업데이트 확인 실패",
                    f"GitHub에 연결할 수 없습니다.\n\n{err}"))
                return
            local_sha = self._load_local_sha()
            if not local_sha:
                local_sha = self._compute_local_sha()
                if local_sha:
                    self._save_local_sha(local_sha)
            self._log(f"  로컬 SHA : {local_sha[:12]}…")
            self._log(f"  원격 SHA : {remote_sha[:12]}…")
            if not local_sha or remote_sha != local_sha:
                def _ask():
                    ret = QMessageBox.question(
                        self, "업데이트 가능",
                        "새로운 업데이트가 있습니다.\n지금 업데이트하시겠습니까?",
                        QMessageBox.Yes | QMessageBox.No)
                    if ret == QMessageBox.Yes:
                        self._do_update(remote_sha, download_url)
                QTimer.singleShot(0, _ask)
            else:
                self._log("✔ 최신 버전입니다.")
                QTimer.singleShot(0, lambda: QMessageBox.information(self, "업데이트", "최신 버전입니다."))
        threading.Thread(target=_run, daemon=True).start()

    def _do_update(self, new_sha: str, download_url: str):
        """main.py를 GitHub에서 다운로드하여 교체 후 재시작."""
        self._log("⬇ 업데이트 다운로드 중...")
        def _run():
            try:
                import urllib.request
                req = urllib.request.Request(
                    download_url,
                    headers={'User-Agent': 'ClipDownloader-Updater'})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
            except Exception as e:
                self._log(f"✘ 다운로드 실패: {e}")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "업데이트 실패", f"다운로드 오류:\n{e}"))
                return
            # 문법 검사
            try:
                import ast
                ast.parse(data.decode('utf-8'))
            except SyntaxError as e:
                self._log(f"✘ 다운로드된 파일 오류: {e}")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "업데이트 실패", f"파일이 손상되었습니다:\n{e}"))
                return
            # 기존 파일 백업 후 교체
            script_path = os.path.abspath(__file__)
            try:
                import shutil
                shutil.copy2(script_path, script_path + '.bak')
                with open(script_path, 'wb') as f:
                    f.write(data)
                self._save_local_sha(new_sha)
                self._log("✔ 업데이트 완료. 재시작합니다...")
                def _restart():
                    QMessageBox.information(self, "업데이트 완료",
                        "업데이트가 완료되었습니다.\n프로그램을 재시작합니다.")
                    import subprocess as _sp
                    _sp.Popen([sys.executable, script_path] + sys.argv[1:])
                    QApplication.quit()
                QTimer.singleShot(0, _restart)
            except Exception as e:
                self._log(f"✘ 파일 교체 실패: {e}")
                QTimer.singleShot(0, lambda: QMessageBox.critical(self, "업데이트 실패",
                    f"파일 교체 중 오류:\n{e}\n\n수동으로 main.py를 교체해주세요."))

    def _update_ytdlp(self):
        self._log("── yt-dlp 업데이트 확인 중... ──")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyRead.connect(lambda: self._log(
            proc.readAll().data().decode('utf-8', errors='replace').strip()))
        proc.finished.connect(lambda code, _: self._log(
            "✔ yt-dlp 업데이트 완료." if code == 0
            else f"✘ 업데이트 실패 (코드: {code})"))
        proc.start(YTDLP_EXE, ['-U'])

    def _show_feature_guide(self):
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QTextBrowser, QPushButton, QTabWidget
        CSS = ("font-size:15px; line-height:1.8;"
               "background:#1e1e1e; color:#e8e8e8;")
        def _tab(html):
            tb = QTextBrowser()
            tb.setOpenExternalLinks(True)
            tb.setStyleSheet(CSS)
            tb.setHtml(f"""
<style>
  body  {{font-size:15px; line-height:1.8; background:#1e1e1e; color:#e8e8e8; margin:14px;}}
  h2   {{color:#7ec8e3; margin-bottom:6px;}}
  h3   {{color:#f0c060; margin-top:18px; margin-bottom:4px; font-size:16px;}}
  b    {{color:#ffffff;}}
  li   {{margin-bottom:6px;}}
  ul   {{padding-left:22px;}}
  .tip {{background:#2a2a2a; border-left:3px solid #7ec8e3;
         padding:8px 12px; border-radius:4px; margin-top:10px; font-size:14px; color:#aaa;}}
</style>
{html}""")
            return tb

        dlg = QDialog(self)
        dlg.setWindowTitle("기능 가이드")
        dlg.resize(680, 620)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(8, 8, 8, 8)

        tabs = QTabWidget()
        tabs.setStyleSheet("QTabBar::tab { font-size:14px; padding:6px 16px; }")

        # ── 탭 1: 영상 로드 ──────────────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>📥 영상 로드</h2>
<h3>URL 모드</h3>
<ul>
  <li>상단 <b>🌐 URL</b> 버튼 선택 후 URL 입력창에 붙여넣고 <b>로드</b> 클릭</li>
  <li>YouTube URL은 클립보드에 복사되면 자동 감지되어 입력창에 채워짐</li>
  <li>최근 로드한 URL 드롭다운에 <b>영상 제목</b>으로 표시되어 재접근 편리</li>
</ul>
<h3>로컬 파일 모드</h3>
<ul>
  <li>상단 <b>📁 로컬 파일</b> 버튼 선택 후 <b>파일 선택</b> 클릭</li>
  <li>미리보기 창 또는 <b>저장 폴더 탭</b>의 파일을 미리보기 창으로 <b>드래그 앤 드롭</b>해도 바로 로드</li>
  <li>H.264 이외 코덱(AV1, HEVC 등) 파일은 자동으로 VP8 변환 후 미리보기</li>
</ul>
<h3>GIF 파일</h3>
<ul>
  <li>GIF 파일 로드 시 자동으로 GIF 모드로 전환</li>
  <li>구간을 잘라 새 GIF로 저장하거나 영상으로 변환 가능</li>
</ul>
<div class='tip'>💡 로드된 영상/파일 정보는 큐에 기억되어, 큐 항목을 더블클릭하면 해당 소스로 다시 로드됩니다.</div>
"""), "📥 영상 로드")

        # ── 탭 2: 구간 설정 ──────────────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>✂️ 구간 설정</h2>
<h3>슬라이더</h3>
<ul>
  <li>왼쪽 핸들: 시작 시간 &nbsp;/&nbsp; 오른쪽 핸들: 종료 시간</li>
  <li>핸들 드래그 중 해당 핸들 위에 <b>구간 길이</b>, 아래에 <b>현재 시간</b> 표시</li>
  <li>핸들을 놓으면 해당 위치로 영상이 자동 이동</li>
</ul>
<h3>시간 직접 입력</h3>
<ul>
  <li>시작/종료 입력창에 <b>HH:MM:SS</b> 형식으로 직접 입력</li>
  <li><b>📍시작</b> / <b>📍종료</b> 버튼: 현재 재생 위치를 구간 시작/종료로 지정</li>
  <li><b>↵ 이동</b> 버튼: 입력한 시간으로 영상 이동</li>
</ul>
<h3>미리보기 플레이어</h3>
<ul>
  <li>미리보기 영역 <b>클릭</b>으로 재생 / 일시정지 전환</li>
  <li><b>반복 재생</b> 체크 시 설정 구간을 계속 반복</li>
  <li><b>재생 속도</b> 조절 가능 (미리보기 속도 = 저장 속도와 연동)</li>
</ul>
<div class='tip'>💡 구간 길이와 형식에 따른 예상 파일 크기가 슬라이더 아래에 표시됩니다. (참고용)</div>
"""), "✂️ 구간 설정")

        # ── 탭 3: 저장 옵션 ──────────────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>💾 저장 옵션</h2>
<h3>형식 / 해상도</h3>
<ul>
  <li><b>형식</b>: mp4 / mkv / webm / mp3 (오디오만) / gif</li>
  <li><b>해상도</b>: 최고화질 / 1080p / 720p / 480p / 360p</li>
  <li><b>소리 제거</b>: 체크 시 무음 영상으로 저장</li>
</ul>
<h3>GIF 옵션</h3>
<ul>
  <li>형식을 <b>gif</b>로 선택하면 FPS와 가로 크기 설정 가능</li>
  <li>FPS가 낮을수록, 크기가 작을수록 파일 용량 감소</li>
</ul>
<h3>재생 속도 (배속 저장)</h3>
<ul>
  <li>0.25x ~ 3.0x 배속으로 저장 가능</li>
  <li>미리보기 재생 속도와 연동되며, 저장된 파일에도 실제로 적용됨</li>
  <li>배속이 1x가 아닐 경우 자동으로 2단계 처리 (다운로드 → ffmpeg 인코딩)</li>
</ul>
<h3>영상 효과 옵션</h3>
<ul>
  <li><b>회전/반전</b>: 90° 시계 / 90° 반시계 / 180° / 좌우 반전 / 상하 반전</li>
  <li><b>볼륨</b>: 0.1x ~ 5.0x 범위로 저장 시 오디오 음량 조절 (1.0 = 원본)</li>
  <li><b>압축(CRF)</b>: 0 = 자동(기본), 값이 클수록 용량↓ 화질↓ &nbsp;(권장: 18~28)</li>
</ul>
<div class='tip'>💡 회전/볼륨/CRF/배속 중 하나라도 기본값이 아니면 URL 다운로드 시 자동으로 2단계 인코딩이 적용됩니다.</div>
<h3>저장 경로 / 파일명</h3>
<ul>
  <li>저장 경로를 지정하지 않으면 <b>download\\</b> 폴더에 자동 저장</li>
  <li>파일명 입력창을 비워두면 자동으로 이름 생성</li>
  <li><b>저장 후 즉시 실행</b> 체크 시 완료 즉시 파일 열기</li>
</ul>
"""), "💾 저장 옵션")

        # ── 탭 4: 저장 큐 ────────────────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>📋 저장 큐</h2>
<h3>기본 사용법</h3>
<ul>
  <li>구간과 옵션을 설정한 뒤 <b>큐에 추가</b> 버튼 클릭</li>
  <li>여러 구간을 등록한 후 <b>큐 실행</b>으로 한 번에 처리</li>
  <li>각 항목은 추가 당시의 소스(URL/파일)와 구간, 길이, 예상 크기를 기억</li>
</ul>
<h3>구간 합치기</h3>
<ul>
  <li><b>구간 합치기</b> 체크 후 큐 실행 시 모든 결과를 하나의 파일로 병합</li>
  <li>등록 순서대로 이어 붙여짐</li>
</ul>
<h3>항목 관리</h3>
<ul>
  <li>항목 <b>더블클릭</b> 또는 우클릭 → <b>재생</b>: 해당 소스와 구간으로 미리보기 로드</li>
  <li>우클릭 → <b>제거</b> 또는 <b>Delete</b> 키로 항목 삭제</li>
  <li>영상 큐와 GIF 큐는 각각 독립적으로 동작</li>
</ul>
<div class='tip'>💡 큐 실행을 시작하면 자동으로 로그 탭으로 전환되어 진행 상황을 확인할 수 있습니다.</div>
"""), "📋 저장 큐")

        # ── 탭 5: 저장 폴더 / 기타 ───────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>📂 저장 폴더 탭</h2>
<ul>
  <li>저장된 파일 목록을 실시간으로 표시 (하위 폴더 포함)</li>
  <li>파일을 미리보기 창으로 <b>드래그</b>하면 바로 로드</li>
  <li>우클릭 메뉴: <b>실행</b> / <b>폴더 열기</b>(파일 선택된 채로 탐색기 열림) / <b>삭제</b> / <b>이름 변경</b></li>
  <li><b>F2</b>: 이름 변경 &nbsp;/&nbsp; <b>Delete</b>: 삭제 &nbsp;/&nbsp; <b>Enter</b>: 파일 열기</li>
  <li>상단 <b>현재 저장 경로</b> 표시 및 <b>폴더 열기</b> 버튼으로 빠른 접근</li>
</ul>

<h2 style='margin-top:22px'>📷 스크린샷</h2>
<ul>
  <li>영상 효과 옵션 행 오른쪽의 <b>📷 스크린샷</b> 버튼 클릭</li>
  <li><b>로컬 파일</b>: 현재 재생 위치의 프레임을 PNG로 정확하게 추출</li>
  <li><b>URL 모드</b>: 플레이어 화면을 캡처하여 PNG로 저장</li>
  <li>저장 위치는 현재 설정된 저장 경로와 동일</li>
</ul>

<h2 style='margin-top:22px'>⚙️ 기타</h2>
<ul>
  <li><b>📌 항상 위</b>: 다른 창 위에 프로그램 고정</li>
  <li>창 크기, 저장 경로, 각종 설정이 자동으로 저장되어 다음 실행 시 복원</li>
  <li>프로그램이 이미 실행 중일 때 재실행하면 추가 실행 여부를 묻는 창이 뜸</li>
</ul>

<h2 style='margin-top:22px'>🔧 도구 메뉴</h2>
<ul>
  <li><b>쿠키 파일 설정</b>: Netscape 형식 쿠키 파일(.txt)을 지정하면 로그인이 필요한 영상도 다운로드 가능</li>
  <li><b>yt-dlp 업데이트</b>: 최신 사이트 지원을 위해 주기적으로 업데이트 권장</li>
  <li><b>ffmpeg 업데이트 방법</b>: 수동 업데이트 안내</li>
</ul>
<div class='tip'>💡 쿠키 파일은 브라우저 확장 프로그램(예: "Get cookies.txt LOCALLY")으로 내보낼 수 있습니다.</div>
"""), "📂 폴더 / 기타")

        # ── 탭 6: 지원 사이트 ────────────────────────────────────────────────
        tabs.addTab(_tab("""
<h2>🌐 지원 사이트</h2>
<p style='color:#aaa;font-size:13px;'>yt-dlp 기반으로 1,000개 이상의 사이트를 지원합니다. 주요 사이트 목록:</p>

<h3>🇰🇷 국내</h3>
<ul>
  <li><b>네이버 TV</b> (tv.naver.com) — 미리보기 불가, 다운로드만 가능</li>
  <li><b>카카오TV</b> (tv.kakao.com)</li>
  <li><b>아프리카TV</b> (afreecatv.com) — VOD</li>
  <li><b>CHZZK</b> (chzzk.naver.com) — 네이버 치지직 VOD</li>
  <li><b>네이버 카페/블로그</b> — 일부 영상</li>
  <li><b>Bugs</b> (bugs.co.kr) — 음악</li>
</ul>

<h3>🌏 글로벌 영상</h3>
<ul>
  <li><b>YouTube</b> (youtube.com, youtu.be) — 숏츠 포함</li>
  <li><b>Instagram</b> (instagram.com) — 릴스, 게시물</li>
  <li><b>TikTok</b> (tiktok.com)</li>
  <li><b>Facebook</b> (facebook.com, fb.watch)</li>
  <li><b>X / Twitter</b> (x.com, twitter.com)</li>
  <li><b>Vimeo</b> (vimeo.com)</li>
  <li><b>Dailymotion</b> (dailymotion.com)</li>
  <li><b>Reddit</b> (reddit.com) — 영상 게시물</li>
  <li><b>Bilibili</b> (bilibili.com)</li>
  <li><b>Niconico</b> (nicovideo.jp)</li>
  <li><b>Twitch</b> (twitch.tv) — VOD / 클립</li>
  <li><b>Kick</b> (kick.com) — VOD / 클립</li>
</ul>

<h3>🎵 오디오 / 음악</h3>
<ul>
  <li><b>SoundCloud</b> (soundcloud.com)</li>
  <li><b>Bandcamp</b> (bandcamp.com)</li>
  <li><b>Mixcloud</b> (mixcloud.com)</li>
</ul>

<h3>📺 방송 / 뉴스</h3>
<ul>
  <li><b>BBC iPlayer</b>, <b>ABC</b>, <b>CBS</b>, <b>CNN</b> 등 일부 방송사</li>
  <li><b>Streamable</b>, <b>Gfycat</b>, <b>Imgur</b> (영상)</li>
</ul>

<div class='tip'>💡 로그인이 필요한 콘텐츠(멤버십, 비공개 등)는 <b>도구 → 쿠키 파일 설정</b>으로 해결할 수 있습니다.<br>
전체 지원 사이트 목록: <a href='https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md'>github.com/yt-dlp/yt-dlp</a></div>
"""), "🌐 지원 사이트")

        lay.addWidget(tabs)
        btn = QPushButton("닫기")
        btn.setFixedWidth(80)
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn, alignment=Qt.AlignRight)
        dlg.exec_()

    def _show_ffmpeg_update_help(self):
        QMessageBox.information(
            self, "ffmpeg 업데이트",
            "ffmpeg는 자동 업데이트를 지원하지 않습니다.\n\n"
            "수동 업데이트 방법:\n"
            "  1. 아래 주소에서 최신 Windows 빌드 다운로드\n"
            "     https://www.gyan.dev/ffmpeg/builds/\n"
            "     (ffmpeg-release-essentials.zip 권장)\n\n"
            "  2. 압축 해제 후 bin\\ffmpeg.exe 파일을\n"
            f"     {FFMPEG_EXE}\n"
            "     위치에 교체하면 됩니다.\n\n"
            "  3. 프로그램을 재시작하면 새 버전이 반영됩니다.")


    # -----------------------------------------------------------------------
    # Queue
    # -----------------------------------------------------------------------
    def _add_to_queue(self):
        if self._mode == 'local':
            url = self._local_file or ''
        else:
            url = (self.url_input.currentData() or self.url_input.currentText()).strip()
        if not url:
            self._log("큐 추가 오류: URL 또는 로컬 파일을 먼저 선택하세요.")
            return
        start_s = self.rslider.start
        end_s   = self.rslider.end
        if end_s <= start_s:
            self._log("큐 추가 오류: 종료 시간이 시작 시간보다 커야 합니다.")
            return
        ext = self.ext_combo.currentText()
        is_gif = (ext == 'gif') or self._local_is_gif
        item_data = {
            'url':       url,
            'mode':      self._mode,
            'start_s':   start_s,
            'end_s':     end_s,
            'res':       self.res_combo.currentText(),
            'ext':       'gif' if is_gif else ext,
            'speed':     self._speed,
            'save_dir':  self.path_input.currentText().strip() or DOWNLOAD_DIR,
            'name':      self.name_input.text().strip(),
            'gif_fps':   self.gif_fps.currentText(),
            'gif_width': self.gif_width.currentText(),
            'mute':      self.mute_chk.isChecked(),
        }
        display_name = os.path.basename(url) if self._mode == 'local' else url
        seg = f"{secs_to_hms(start_s)} ~ {secs_to_hms(end_s)}"
        clip_secs = end_s - start_s
        dur = secs_to_duration(clip_secs)
        local_f = url if self._mode == 'local' else ''
        size_bytes = self._calc_size_bytes(
            clip_secs, 'gif' if is_gif else item_data['ext'],
            item_data.get('res', '최고화질'), item_data.get('gif_width', '480'),
            item_data.get('mute', False), local_f, self._duration)
        size_str = f"~{self._size_str(size_bytes)}"
        if is_gif:
            self._gif_queue.append(item_data)
            idx = len(self._gif_queue)
            row = QTreeWidgetItem()
            row.setText(0, str(idx))
            row.setText(1, display_name)
            row.setText(2, seg)
            row.setText(3, dur)
            row.setText(4, size_str)
            fps = item_data['gif_fps']; w = item_data['gif_width']
            row.setText(5, f"{fps}fps/{w}")
            row.setText(6, "대기")
            self.gif_queue_tree.addTopLevelItem(row)
            self._log(f"GIF 큐 추가: #{idx}  [{seg}]  {dur}  {size_str}  {fps}fps/{w}")
        else:
            self._queue.append(item_data)
            idx = len(self._queue)
            row = QTreeWidgetItem()
            row.setText(0, str(idx))
            row.setText(1, display_name)
            row.setText(2, seg)
            row.setText(3, dur)
            row.setText(4, size_str)
            row.setText(5, ext)
            row.setText(6, "대기")
            self.queue_tree.addTopLevelItem(row)
            self._log(f"큐 추가: #{idx}  [{seg}]  {dur}  {size_str}  {ext}")

    def _run_queue(self):
        if self._queue_running:
            return
        if not self._queue:
            self._log("큐가 비어 있습니다.")
            return
        self._queue_running = True
        self._queue_idx     = 0
        self._queue_results = []
        self._queue_result_durations = {}
        self.dl_btn.setEnabled(False)
        self.queue_add_btn.setEnabled(False)
        self._queue_run_btn.setEnabled(False)
        self._queue_clear_btn.setEnabled(False)
        self._gif_queue_run_btn.setEnabled(False)
        self._gif_queue_clear_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._right_tabs.setCurrentIndex(1)  # 로그 탭으로 전환
        self._log(f"── 큐 실행 시작: 총 {len(self._queue)}개 항목 ──")
        self._run_next_queue_item()

    def _run_next_queue_item(self):
        if self._queue_idx >= len(self._queue):
            self._finish_queue()
            return
        self.cancel_btn.setEnabled(True)
        item_data = self._queue[self._queue_idx]
        tree_item = self.queue_tree.topLevelItem(self._queue_idx)
        if tree_item:
            tree_item.setText(6, "실행 중")
        self._log(f"── 큐 #{self._queue_idx + 1} 시작 ──")
        self._start_download_queue_item(item_data)

    def _start_download_queue_item(self, item: dict):
        import time as _time
        self._dl_start_time   = _time.time()
        self._last_saved_path = None

        url      = item['url']
        mode     = item['mode']
        start_s  = item['start_s']
        end_s    = item['end_s']
        res      = item['res']
        ext      = item['ext']
        speed    = item['speed']
        save_dir = item['save_dir']
        name     = item['name']
        self._current_item_duration = max(0.001, end_s - start_s)

        self._mode        = mode
        self._speed       = speed
        self._local_file  = url if mode == 'local' else self._local_file

        self.rslider.setEnd(end_s)
        self.rslider.setStart(start_s)
        self._upd_start = True
        self.start_in.setText(secs_to_hms(start_s))
        self._upd_start = False
        self._upd_end = True
        self.end_in.setText(secs_to_hms(end_s))
        self._upd_end = False
        self.path_input.setCurrentText(save_dir)
        self.name_input.setText(name)
        self.res_combo.setCurrentText(res)
        self.ext_combo.setCurrentText(ext)
        self.gif_fps.setCurrentText(item.get('gif_fps', '10'))
        self.gif_width.setCurrentText(item.get('gif_width', '480'))

        start_str = secs_to_hms(start_s)
        end_str   = secs_to_hms(end_s)
        mute = item.get('mute', False)
        fmt_args, actual_ext = build_format_args(res, ext, mute=mute)

        os.makedirs(save_dir, exist_ok=True)
        self._clip_secs = max(1, end_s - start_s)

        self.progress.setRange(0, 0)
        self.progress.setValue(0)

        if mode == 'local':
            self._start_local_clip(start_str, end_str, actual_ext, save_dir)
        elif actual_ext == 'gif':
            self._start_gif_stage1(url, start_str, end_str, save_dir)
        elif abs(speed - 1.0) > 0.001:
            from datetime import datetime
            ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = name if name else f'clip_{ts}'
            stem = re.sub(r'[\\/:*?"<>|]', '_', stem)
            self._speed_save_dir   = save_dir
            self._speed_actual_ext = actual_ext
            self._speed_final_out  = unique_path(os.path.join(save_dir, f"{stem}.{actual_ext}"))
            self._speed_temp       = None
            temp_args = (
                ['--download-sections', f'*{start_str}-{end_str}']
                + fmt_args
                + ['--ffmpeg-location', FFMPEG_DIR,
                   '-P', save_dir,
                   '-o', f'_clipdl_speed_temp_{os.getpid()}.%(ext)s',
                   '--windows-filenames',
                   '--no-part',
                   url]
            )
            self._run_process(YTDLP_EXE, temp_args, self._on_url_speed_dl_done)
        else:
            args = (
                ['--download-sections', f'*{start_str}-{end_str}']
                + fmt_args
                + ['--ffmpeg-location', FFMPEG_DIR,
                   '-P', save_dir,
                   '-o', self._unique_output(save_dir, actual_ext),
                   '--windows-filenames',
                   '--no-part',
                   url]
            )
            self._run_process(YTDLP_EXE, args, self._on_done)

    def _on_queue_item_done(self, path: str, success: bool):
        tree_item = self.queue_tree.topLevelItem(self._queue_idx)
        if success:
            if tree_item:
                tree_item.setText(6, "✔완료")
            if path:
                self._queue_results.append(path)
                dur = getattr(self, '_current_item_duration', 0)
                if dur > 0:
                    self._queue_result_durations[path] = dur
        else:
            if tree_item:
                tree_item.setText(6, "✘실패")
        self._queue_idx += 1
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._run_next_queue_item()

    def _finish_queue(self):
        self._log(f"── 큐 실행 완료 (성공 {len(self._queue_results)}개) ──")
        if self._queue_merge_chk.isChecked() and len(self._queue_results) >= 2:
            self._concat_queue_results()
        else:
            if self._queue_merge_chk.isChecked() and len(self._queue_results) < 2:
                self._log(f"⚠ 합치기 건너뜀: 성공한 파일이 {len(self._queue_results)}개뿐입니다.")
            self._end_queue()

    def _concat_queue_results(self):
        results = self._queue_results
        first_path = results[0]
        first_ext  = os.path.splitext(first_path)[1]
        base_stem  = os.path.splitext(os.path.basename(first_path))[0]
        save_dir   = os.path.dirname(first_path)
        merged_path = unique_path(os.path.join(save_dir, f"{base_stem}_merged{first_ext}"))

        list_path = os.path.join(save_dir, f'_clipdl_concat_{os.getpid()}.txt')
        try:
            with open(list_path, 'w', encoding='utf-8') as f:
                for i, p in enumerate(results):
                    safe = p.replace('\\', '/').replace("'", "\\'")
                    f.write(f"file '{safe}'\n")
                    # duration 지시어: concat demuxer가 각 클립의 길이를 정확히 알게 해
                    # AV1 코덱 딜레이로 인한 DTS 오프셋 오류를 방지한다.
                    # 마지막 세그먼트는 지정 불필요 (컨테이너에서 읽음)
                    if i < len(results) - 1:
                        dur = self._queue_result_durations.get(p, 0)
                        if dur > 0:
                            f.write(f"duration {dur:.6f}\n")
        except Exception as e:
            self._log(f"✘ concat 리스트 생성 실패: {e}")
            self._end_queue()
            return

        self._concat_list_path = list_path
        self._concat_out_path  = merged_path
        self._concat_tmp_path  = None

        self._log(f"구간 합치기 중 ({len(results)}개) → {os.path.basename(merged_path)}")
        concat_proc = QProcess(self)
        concat_proc.setWorkingDirectory(YTDLP_DIR)
        concat_proc.setProcessChannelMode(QProcess.MergedChannels)
        concat_proc.readyRead.connect(
            lambda p=concat_proc: self._log(
                p.readAll().data().decode('utf-8', errors='replace').strip()))
        concat_proc.finished.connect(self._on_concat_done)
        _cargs = ['-y', '-f', 'concat', '-safe', '0',
                  '-i', list_path, '-c', 'copy', merged_path]
        concat_proc.start(FFMPEG_EXE, _cargs)
        self._concat_proc = concat_proc

    def _on_concat_done(self, code: int, _status):
        list_path = getattr(self, '_concat_list_path', '')
        out_path  = getattr(self, '_concat_out_path', '')
        tmp_path  = getattr(self, '_concat_tmp_path', None)
        if list_path and os.path.exists(list_path):
            try:
                os.remove(list_path)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        if code == 0 and out_path:
            # 합치기 성공 → 개별 클립 삭제
            for p in self._queue_results:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self._new_file_paths.add(out_path)
            self._log_link("✔ 합치기 완료! 저장 위치: ", out_path)
            self._refresh_file_list()
        else:
            self._log(f"✘ 합치기 실패 (코드: {code})")
        self._end_queue()

    def _end_queue(self):
        self._queue_running = False
        self._queue_results = []
        self._queue_result_durations.clear()
        self.dl_btn.setEnabled(True)
        self.queue_add_btn.setEnabled(True)
        self._queue_run_btn.setEnabled(True)
        self._queue_clear_btn.setEnabled(True)
        self._gif_queue_run_btn.setEnabled(not self._gif_queue_running)
        self._gif_queue_clear_btn.setEnabled(not self._gif_queue_running)
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._log("── 큐 종료 ──")

    def _clear_queue(self):
        if self._queue_running:
            self._log("큐 실행 중에는 초기화할 수 없습니다.")
            return
        self._queue.clear()
        self._queue_results.clear()
        self._queue_result_durations.clear()
        self.queue_tree.clear()
        self._log("큐가 초기화되었습니다.")

    # -----------------------------------------------------------------------
    # GIF 큐 실행
    # -----------------------------------------------------------------------
    def _run_gif_queue(self):
        if self._gif_queue_running:
            return
        if not self._gif_queue:
            self._log("GIF 큐가 비어 있습니다.")
            return
        self._gif_queue_running = True
        self._gif_queue_idx     = 0
        self._gif_queue_results = []
        self.dl_btn.setEnabled(False)
        self.queue_add_btn.setEnabled(False)
        self._gif_queue_run_btn.setEnabled(False)
        self._gif_queue_clear_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self._right_tabs.setCurrentIndex(1)  # 로그 탭으로 전환
        self._log(f"── GIF 큐 실행 시작: 총 {len(self._gif_queue)}개 항목 ──")
        self._run_next_gif_queue_item()

    def _run_next_gif_queue_item(self):
        if self._gif_queue_idx >= len(self._gif_queue):
            self._finish_gif_queue()
            return
        # 항목 전환 시 cancel_btn 항상 활성화 (취소 후 재진입 시 비활성 방지)
        self.cancel_btn.setEnabled(True)
        item_data = self._gif_queue[self._gif_queue_idx]
        tree_item = self.gif_queue_tree.topLevelItem(self._gif_queue_idx)
        if tree_item:
            tree_item.setText(6, "실행 중")
        self._log(f"── GIF 큐 #{self._gif_queue_idx + 1} 시작 ──")
        self._start_gif_queue_item(item_data)

    def _start_gif_queue_item(self, item: dict):
        import time as _time
        self._dl_start_time   = _time.time()
        self._last_saved_path = None

        url      = item['url']
        mode     = item['mode']
        start_s  = item['start_s']
        end_s    = item['end_s']
        save_dir = item['save_dir']
        name     = item['name']

        self._mode       = mode
        self._speed      = item.get('speed', 1.0)
        self._local_file = url if mode == 'local' else self._local_file

        self.rslider.setEnd(end_s)
        self.rslider.setStart(start_s)
        self._upd_start = True
        self.start_in.setText(secs_to_hms(start_s))
        self._upd_start = False
        self._upd_end = True
        self.end_in.setText(secs_to_hms(end_s))
        self._upd_end = False
        self.path_input.setCurrentText(save_dir)
        self.name_input.setText(name)
        self.gif_fps.setCurrentText(item.get('gif_fps', '10'))
        self.gif_width.setCurrentText(item.get('gif_width', '480'))

        os.makedirs(save_dir, exist_ok=True)
        self._clip_secs = max(1, end_s - start_s)
        self.progress.setRange(0, 0)
        self.progress.setValue(0)

        start_str = secs_to_hms(start_s)
        end_str   = secs_to_hms(end_s)
        if mode == 'local':
            # 로컬 GIF: 2단계
            # 1/2: 클립 추출 + 다운스케일(libx264) — AV1/HEVC 등 무거운 코덱 1회만 디코딩
            from datetime import datetime
            ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem  = self._get_output_name('gif', f'clip_{ts}')
            gif   = unique_path(os.path.join(save_dir, f"{stem}.gif"))
            dur_s = hms_to_secs(end_str) - hms_to_secs(start_str)
            fps   = item.get('gif_fps', '10')
            w     = item.get('gif_width', '480')
            scale_vf = f"scale={w}:-2:flags=lanczos" if w != '원본' else \
                       "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            self._gif_queue_temp = os.path.join(
                save_dir, f'_gifq_temp_{os.getpid()}_{ts}.mp4')
            args1 = ['-y', '-ss', start_str, '-i', url,
                     '-t', str(dur_s), '-vf', scale_vf,
                     '-c:v', 'libx264', '-crf', '18', '-preset', 'fast',
                     '-an', self._gif_queue_temp]
            self._log("1/2  클립 추출 중 (다운스케일)...")
            self._run_process(
                FFMPEG_EXE, args1,
                lambda c, s, g=gif, f=fps: self._on_gif_queue_extract_done(c, s, g, f))
        else:
            self._start_gif_stage1(url, start_str, end_str, save_dir)

    def _cleanup_gif_queue_temp(self):
        tmp = getattr(self, '_gif_queue_temp', None)
        if tmp:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            self._gif_queue_temp = None

    def _on_gif_queue_extract_done(self, code: int, _status, gif_path: str, fps: str):
        """로컬 GIF 큐 1단계(클립 추출) 완료 → 2단계(GIF 변환) 시작."""
        tmp = getattr(self, '_gif_queue_temp', None)
        if code != 0 or not tmp or not os.path.exists(tmp):
            self._cleanup_gif_queue_temp()
            self._log(f"✘ 클립 추출 실패 (코드: {code})")
            self._on_gif_queue_item_done('', False)
            return
        vf = (f"fps={fps},split[s0][s1];"
              f"[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
        args2 = ['-y', '-i', tmp, '-vf', vf, '-r', fps, '-loop', '0', gif_path]
        self._log("2/2  GIF 변환 중...")
        self.progress.setRange(0, 0)
        self._run_process(
            FFMPEG_EXE, args2,
            lambda c, s, g=gif_path: self._on_gif_queue_stage_done(c, s, g))

    def _on_gif_queue_stage_done(self, code: int, _status, gif_path: str):
        """로컬 GIF 큐 2단계(GIF 변환) 완료 콜백."""
        self._cleanup_gif_queue_temp()
        if code == 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self._new_file_paths.add(gif_path)
            self._log_link("✔ GIF 완료!  저장 위치: ", gif_path)
            self._refresh_file_list()
        else:
            self._log(f"✘ GIF 변환 실패 (코드: {code})")
        self._on_gif_queue_item_done(gif_path if code == 0 else '', code == 0)

    def _on_gif_queue_item_done(self, path: str, success: bool):
        tree_item = self.gif_queue_tree.topLevelItem(self._gif_queue_idx)
        if success:
            if tree_item:
                tree_item.setText(6, "✔완료")
            if path:
                self._gif_queue_results.append(path)
        else:
            if tree_item:
                tree_item.setText(6, "✘실패")
        self._gif_queue_idx += 1
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._run_next_gif_queue_item()

    def _finish_gif_queue(self):
        self._log("── GIF 큐 실행 완료 ──")
        if self._gif_queue_merge_chk.isChecked() and len(self._gif_queue_results) >= 2:
            self._concat_gif_queue_results()
        else:
            self._end_gif_queue()

    def _concat_gif_queue_results(self):
        """완료된 GIF 파일들을 ffmpeg로 연결 (팔레트 재생성)."""
        results     = self._gif_queue_results
        first_path  = results[0]
        save_dir    = os.path.dirname(first_path)
        base_stem   = os.path.splitext(os.path.basename(first_path))[0]
        merged_path = unique_path(os.path.join(save_dir, f"{base_stem}_merged.gif"))

        list_path = os.path.join(save_dir, f'_clipdl_gif_concat_{os.getpid()}.txt')
        try:
            with open(list_path, 'w', encoding='utf-8') as f:
                for p in results:
                    safe = p.replace('\\', '/').replace("'", "\\'")
                    f.write(f"file '{safe}'\n")
        except Exception as e:
            self._log(f"✘ GIF concat 리스트 생성 실패: {e}")
            self._end_gif_queue()
            return

        self._gif_concat_list_path = list_path
        self._gif_concat_out_path  = merged_path
        self._log(f"GIF 구간 합치기 중 ({len(results)}개) → {os.path.basename(merged_path)}")
        proc = QProcess(self)
        proc.setWorkingDirectory(YTDLP_DIR)
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyRead.connect(
            lambda p=proc: self._log(
                p.readAll().data().decode('utf-8', errors='replace').strip()))
        proc.finished.connect(self._on_gif_concat_done)
        # GIF는 스트림 복사 불가 — concat demuxer로 읽은 뒤 팔레트 재생성하여 재인코딩
        vf = ("split[s0][s1];"
              "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer")
        args = ['-y', '-f', 'concat', '-safe', '0', '-i', list_path,
                '-vf', vf, '-loop', '0', merged_path]
        proc.start(FFMPEG_EXE, args)
        self._gif_concat_proc = proc

    def _on_gif_concat_done(self, code: int, _status):
        list_path = getattr(self, '_gif_concat_list_path', '')
        out_path  = getattr(self, '_gif_concat_out_path', '')
        if list_path and os.path.exists(list_path):
            try:
                os.remove(list_path)
            except Exception:
                pass
        if code == 0 and out_path:
            # 합치기 성공 → 개별 GIF 삭제
            for p in self._gif_queue_results:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            self._new_file_paths.add(out_path)
            self._log_link("✔ GIF 합치기 완료! 저장 위치: ", out_path)
            self._refresh_file_list()
        else:
            self._log(f"✘ GIF 합치기 실패 (코드: {code})")
        self._end_gif_queue()

    def _end_gif_queue(self):
        self._gif_queue_running = False
        self._gif_queue_results = []
        self.dl_btn.setEnabled(True)
        self.queue_add_btn.setEnabled(True)
        self._gif_queue_run_btn.setEnabled(True)
        self._gif_queue_clear_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._log("── GIF 큐 종료 ──")

    def _clear_gif_queue(self):
        if self._gif_queue_running:
            self._log("GIF 큐 실행 중에는 초기화할 수 없습니다.")
            return
        self._gif_queue.clear()
        self._gif_queue_results.clear()
        self.gif_queue_tree.clear()
        self._log("GIF 큐가 초기화되었습니다.")

    def _on_queue_context(self, pos):
        item = self.queue_tree.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("재생", lambda: self._on_queue_item_double_click(item, 0))
        menu.addAction("제거", lambda: self._remove_queue_item(item))
        menu.exec_(self.queue_tree.viewport().mapToGlobal(pos))

    def _remove_queue_item(self, item: QTreeWidgetItem):
        if self._queue_running:
            self._log("큐 실행 중에는 항목을 제거할 수 없습니다.")
            return
        idx = self.queue_tree.indexOfTopLevelItem(item)
        if idx < 0:
            return
        self.queue_tree.takeTopLevelItem(idx)
        if idx < len(self._queue):
            self._queue.pop(idx)
        # 번호 갱신
        for i in range(self.queue_tree.topLevelItemCount()):
            self.queue_tree.topLevelItem(i).setText(0, str(i + 1))

    def _queue_tree_key_press(self, ev):
        if ev.key() == Qt.Key_Delete:
            item = self.queue_tree.currentItem()
            if item:
                self._remove_queue_item(item)
        else:
            QTreeWidget.keyPressEvent(self.queue_tree, ev)

    def _on_gif_queue_context(self, pos):
        item = self.gif_queue_tree.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("재생", lambda: self._on_gif_queue_item_double_click(item, 0))
        menu.addAction("제거", lambda: self._remove_gif_queue_item(item))
        menu.exec_(self.gif_queue_tree.viewport().mapToGlobal(pos))

    def _remove_gif_queue_item(self, item: QTreeWidgetItem):
        if self._gif_queue_running:
            self._log("GIF 큐 실행 중에는 항목을 제거할 수 없습니다.")
            return
        idx = self.gif_queue_tree.indexOfTopLevelItem(item)
        if idx < 0:
            return
        self.gif_queue_tree.takeTopLevelItem(idx)
        if idx < len(self._gif_queue):
            self._gif_queue.pop(idx)
        for i in range(self.gif_queue_tree.topLevelItemCount()):
            self.gif_queue_tree.topLevelItem(i).setText(0, str(i + 1))

    def _gif_queue_tree_key_press(self, ev):
        if ev.key() == Qt.Key_Delete:
            item = self.gif_queue_tree.currentItem()
            if item:
                self._remove_gif_queue_item(item)
        else:
            QTreeWidget.keyPressEvent(self.gif_queue_tree, ev)

    def _on_queue_item_double_click(self, item: QTreeWidgetItem, _col: int):
        idx = self.queue_tree.indexOfTopLevelItem(item)
        if idx < 0 or idx >= len(self._queue):
            return
        data = self._queue[idx]
        self._pending_segment = (data['start_s'], data['end_s'])
        if data['mode'] == 'local':
            self._on_file_dropped_to_player(data['url'])
        else:
            self._set_source(0)   # URL 모드
            self.url_input.setCurrentText(data['url'])
            self.load_video()

    def _on_gif_queue_item_double_click(self, item: QTreeWidgetItem, _col: int):
        idx = self.gif_queue_tree.indexOfTopLevelItem(item)
        if idx < 0 or idx >= len(self._gif_queue):
            return
        data = self._gif_queue[idx]
        self._pending_segment = (data['start_s'], data['end_s'])
        if data['mode'] == 'local':
            self._on_file_dropped_to_player(data['url'])
        else:
            self._set_source(0)   # URL 모드
            self.url_input.setCurrentText(data['url'])
            self.load_video()

    # -----------------------------------------------------------------------
    # Clipboard URL auto-detect
    # -----------------------------------------------------------------------
    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QEvent.ActivationChange and self.isActiveWindow():
            self._check_clipboard_url()

    def _check_clipboard_url(self):
        if self._mode != 'url':
            return
        text = QApplication.clipboard().text().strip()
        if not text or not is_youtube(text):
            return
        # url_input과 다른 YouTube URL이 클립보드에 있으면 항상 자동 입력
        if text != self.url_input.currentText().strip():
            self.url_input.setCurrentText(text)
            self._log("📋 클립보드 URL 자동 입력")

    # -----------------------------------------------------------------------
    # Close
    # -----------------------------------------------------------------------
    def closeEvent(self, ev):
        self._closing = True
        self._save_settings()
        self._poll_timer.stop()
        self._dur_timer.stop()
        # 실행 중인 모든 프로세스: 신호 끊고 kill → waitForFinished
        _procs = [
            self._process,
            getattr(self, '_gif_concat_proc', None),
            getattr(self, '_concat_proc', None),
            getattr(self, '_proxy_proc', None),
            getattr(self, '_url_info_proc', None),
        ]
        for proc in _procs:
            if proc and proc.state() != QProcess.NotRunning:
                try:
                    proc.finished.disconnect()
                except Exception:
                    pass
                proc.kill()
                proc.waitForFinished(2000)
        # 임시 파일 정리
        for tmp_attr in ('_preview_proxy', '_concat_tmp_path', '_gif_concat_list_path'):
            tmp = getattr(self, tmp_attr, '')
            if tmp and os.path.isfile(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        super().closeEvent(ev)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    import shutil as _shutil

    # PID 기반 격리 디렉터리 — 다중 인스턴스 LevelDB 잠금 충돌 방지
    pid = os.getpid()
    web_base = os.path.join(BASE_DIR, '.webengine')
    web_dir  = os.path.join(web_base, str(pid))
    os.makedirs(web_dir, exist_ok=True)

    # 종료된 인스턴스의 잔여 폴더 정리 (PID 폴더 중 실행 중이 아닌 것)
    try:
        import psutil
        _have_psutil = True
    except ImportError:
        _have_psutil = False
    if os.path.isdir(web_base):
        for entry in os.scandir(web_base):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            old_pid = int(entry.name)
            if old_pid == pid:
                continue
            alive = False
            if _have_psutil:
                alive = psutil.pid_exists(old_pid)
            else:
                # psutil 없으면 os.kill(pid, 0) 로 확인
                try:
                    os.kill(old_pid, 0)
                    alive = True
                except (OSError, ProcessLookupError):
                    alive = False
            if not alive:
                try:
                    _shutil.rmtree(entry.path, ignore_errors=True)
                except Exception:
                    pass

    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (
        '--disk-cache-dir=' + web_dir.replace('\\', '/') +
        ' --no-sandbox'
        ' --disable-gpu-shader-disk-cache'
    )

    # QtWebEngine 내부 경고 억제 (기능에 영향 없음)
    os.environ.setdefault('QT_LOGGING_RULES',
        '*.debug=false;qt.webenginecontext=false;'
        'qt.webengine.error=false;js=false')

    global _HTML_BYTES, _WIN_MUTEX
    _HTML_BYTES = HTML_TEMPLATE.encode('utf-8')
    _start_server()

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # ── 다중 인스턴스 감지 (Windows 네임드 뮤텍스) ───────────────────────────
    try:
        import ctypes
        _WIN_MUTEX = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Global\\ClipDownloader_aram_v1")
        already_running = (ctypes.windll.kernel32.GetLastError() == 183)  # ERROR_ALREADY_EXISTS
    except Exception:
        already_running = False

    if already_running:
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            None,
            "이미 실행 중",
            "Clip Downloader가 이미 실행 중입니다.\n\n"
            "추가로 실행하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.No:
            sys.exit(0)

    # 앱 전용 저장소/캐시 경로 적용
    from PyQt5.QtWebEngineWidgets import QWebEngineProfile
    profile = QWebEngineProfile.defaultProfile()
    profile.setPersistentStoragePath(web_dir)
    profile.setCachePath(os.path.join(web_dir, 'cache'))

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
