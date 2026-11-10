"""表情包图库与选图逻辑。

借鉴 whale_girl 的核心机制：模型在回复里写 [[sticker:分类]]，
机器人解析后从对应分类随机挑一张发出去，标记本身不进正文。
本模块只做纯逻辑（无 GUI、无网络），便于离线测试。
"""
from __future__ import annotations

import random
import re
from pathlib import Path

EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
ANIMATED_EXTS = {'.gif'}
MAX_BYTES = 64 * 1024 * 1024

MARKER = re.compile(r'\[\[\s*sticker\s*[:：]\s*([^\]\r\n]{1,24})\s*\]\]', re.I)

# 明确索要表情包（借鉴 whale_girl 的 WANT_RE / REVERSE_RE，做了简化）
WANT = re.compile(
    r'(发|来|甩|扔|给|丢|整|弄|上|换)(一?个|一?张|一?只|点|张)?\s*'
    r'(表情包?|动图|gif|贴纸|梗图|meme|图)', re.I)
REVERSE = re.compile(r'(表情包?|动图|贴纸|梗图|meme)\s*(来|发|整|甩|扔|走起)', re.I)
BARE = re.compile(r'^\s*(表情包|表情|动图|gif)\s*(呗|吧|啊|呀|嘛|啦)?\s*[！!？?~～。]*\s*$', re.I)
NEGATIVE = re.compile(r'(不要|别|不用|不想|不许|不准|停止|别再)[^，。！？!?\n]{0,10}'
                      r'(发|来|给|整|弄|甩|扔|上|用|拿)', re.I)
ANIMATED_WORD = re.compile(r'动图|gif|会动|动画', re.I)

# 严肃场景不发图（只收明显的坏消息/求助，避免日常闲聊被误判）
SERIOUS = re.compile(
    r'失恋|分手|离婚|生病|住院|医院|去世|被裁|裁员|失业|焦虑|抑郁|委屈|想哭|'
    r'撑不住|难受|求助|怎么办|出事|被骂|挨骂|欺负|伤心|难过|不开心', re.I)

# 模型可能写的别名 -> 图库目录名
SYNONYMS = {
    '开心': ('高兴', '笑', '哈哈', '搞笑', '好笑', '乐', '鼓掌', '庆祝', '哈哈大笑'),
    '傲娇': ('可爱', '萌', '撒娇', '害羞', '亲亲', '口是心非'),
    '炸毛': ('愤怒', '火大', '暴躁', '烦', '讨厌', '气炸'),
    '生气': ('气', '恼', '怒'),
    '无奈': ('摊手', '没办法', '叹气'),
    '无语': ('沉默', '汗', '服了'),
    '崩溃': ('裂开', '崩了', '受不了'),
    '摸鱼': ('划水', '躺平', '偷懒'),
    '打工': ('上班', '加班', '搬砖', '牛马'),
    '加油': ('打气', '鼓励', '冲'),
}

FALLBACK_ORDER = ('开心', '傲娇', '炸毛', '无奈', '无语', '摸鱼', '打工', '加油', '生气', '崩溃')


def load_library(root: str | Path) -> dict[str, list[Path]]:
    """扫描图库一级子目录，返回 {分类名: [图片路径]}。只读、不递归。"""
    base = Path(root)
    lib: dict[str, list[Path]] = {}
    if not base.is_dir():
        return lib
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith('.'):
            continue
        files = [p for p in sorted(child.iterdir())
                 if p.is_file() and p.suffix.lower() in EXTS and 0 < p.stat().st_size <= MAX_BYTES]
        if files:
            lib[child.name] = files
    return lib


def categories(lib: dict[str, list[Path]]) -> list[str]:
    ordered = [c for c in FALLBACK_ORDER if c in lib]
    ordered += [c for c in lib if c not in ordered]
    return ordered


def normalize(name: str, lib: dict[str, list[Path]]) -> str | None:
    """模型写的分类名 -> 真实目录名。精确 -> 别名 -> 子串。"""
    raw = (name or '').strip()
    if not raw:
        return None
    if raw in lib:
        return raw
    low = raw.lower()
    for label, words in SYNONYMS.items():
        if label in lib and (low == label.lower() or low in words or
                             any(w == low or w in low or low in w for w in words)):
            return label
    for label in lib:
        if label in raw or raw in label:
            return label
    return None


def pick(lib: dict[str, list[Path]], category: str, recent: list[str] | None = None,
         animated: bool | None = False, rng=random) -> tuple[Path, str] | None:
    """挑一张图，返回 (路径, 实际分类)。分类不存在时按 FALLBACK_ORDER 回退。

    animated=None 表示不限；False 优先静态图；True 优先 GIF。
    recent 是最近发过的路径字符串列表，尽量避开。
    """
    if not lib:
        return None
    wanted = normalize(category, lib)
    order = ([wanted] if wanted else []) + [c for c in FALLBACK_ORDER if c != wanted]
    order += [c for c in lib if c not in order]
    recent = recent or []
    for cat in order:
        pool = lib.get(cat) or []
        if animated is True:
            gifs = [p for p in pool if p.suffix.lower() in ANIMATED_EXTS]
            pool = gifs or pool
        elif animated is False:
            stills = [p for p in pool if p.suffix.lower() not in ANIMATED_EXTS]
            pool = stills or pool
        if not pool:
            continue
        fresh = [p for p in pool if str(p) not in recent]
        options = fresh or ([p for p in pool if str(p) != recent[-1]] if len(pool) > 1 else pool)
        if options:
            return options[min(len(options) - 1, int(rng.random() * len(options)))], cat
    return None


def parse(text: str) -> tuple[str, str | None]:
    """从模型回复里剥掉标记，返回 (正文, 分类)。多个标记只取第一个。"""
    if not text:
        return '', None
    found = MARKER.search(text)
    clean = MARKER.sub('', text).strip()
    return clean, (found.group(1).strip() if found else None)


def is_request(text: str) -> bool:
    t = (text or '').strip()
    if not t or NEGATIVE.search(t):
        return False
    return bool(WANT.search(t) or REVERSE.search(t) or BARE.match(t))


def is_serious(text: str) -> bool:
    return bool(SERIOUS.search(text or ''))


def wants_animated(text: str) -> bool:
    return bool(ANIMATED_WORD.search(text or ''))


def prompt_hint(lib: dict[str, list[Path]]) -> str:
    cats = '、'.join(categories(lib))
    return (
        '\n\n【表情包】本轮你可以顺便发一张表情包。如果合适，就在回复的最后'
        f'追加 [[sticker:分类]]，分类只能从这些里选：{cats}。\n'
        '标记不要写进正文、不要解释它；不想发就不要加。'
        '发图时正文也要说一句话，不要只输出标记。'
        '用户明确说「动图」时优先挑会动的。'
    )


def fast_send(path: Path, wait: float = 0.8) -> None:
    """轻量发图：CF_HDROP 剪贴板 + 一次点击/粘贴/回车。

    借鉴 whale_girl/bridge/sticker_transport.py 的取舍：刻意不做
    ensure_visible（会最小化别的窗口）、open_chat（OCR 重扫侧栏）、
    get_input_box（失败会重校准）、像素草稿轮询（最多 3x6 秒）。
    代价是要求调用方保证：微信主窗口在前台、目标会话已打开、输入框为空。
    前置条件不满足时抛 RuntimeError，由调用方回退到标准发送路径。
    """
    import time
    import win32api
    import win32con
    import win32gui
    from wechatauto.guia import WeChatGUI  # 只用它的静态剪贴板方法，不构造实例

    found = []

    def _cb(hwnd, _):
        try:
            if (win32gui.IsWindowVisible(hwnd)
                    and win32gui.GetWindowText(hwnd) == '微信'
                    and win32gui.GetClassName(hwnd).startswith('Qt')):
                found.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(_cb, None)
    if not found:
        raise RuntimeError('找不到微信主窗口')
    hwnd = found[-1]
    if win32gui.IsIconic(hwnd):
        raise RuntimeError('微信窗口已最小化')
    if win32gui.GetForegroundWindow() != hwnd:
        # 只做一次轻量置前，不调用 ensure_visible()（它会最小化别的窗口，很慢且打扰）
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.15)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() != hwnd:
            raise RuntimeError('微信不在前台')

    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    x, y = l + int(w * 0.67), t + int(h * 0.82)
    if not (0 <= x < sw and 0 <= y < sh):
        raise RuntimeError(f'输入框坐标 ({x},{y}) 超出屏幕 {sw}x{sh}')

    if not WeChatGUI.copy_files_to_clipboard([str(path)]):
        raise RuntimeError('CF_HDROP 剪贴板写入失败')

    win32api.SetCursorPos((x, y))
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.12)
    for vk, flags in ((win32con.VK_CONTROL, 0), (ord('V'), 0),
                      (ord('V'), win32con.KEYEVENTF_KEYUP),
                      (win32con.VK_CONTROL, win32con.KEYEVENTF_KEYUP)):
        win32api.keybd_event(vk, 0, flags, 0)
    time.sleep(max(0.2, wait))  # 等缩略图渲染，远短于库的 6 秒像素轮询
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)


QUICK_LINES = ('喏，给你。', '哼，拿去。', '就这一张哦。', '别嫌少啊。', '接着啦。')


def validate(path: Path, root: str | Path) -> bool:
    """发送前复核：必须是图库目录内的普通文件，且格式/大小合法。"""
    try:
        base = Path(root).resolve()
        real = Path(path).resolve()
        if base not in real.parents or not real.is_file():
            return False
        if real.suffix.lower() not in EXTS or not 0 < real.stat().st_size <= MAX_BYTES:
            return False
        head = real.open('rb').read(8)
        if real.suffix.lower() == '.gif':
            return head[:6] in (b'GIF87a', b'GIF89a')
        if real.suffix.lower() == '.png':
            return head.startswith(b'\x89PNG\r\n\x1a\n')
        if real.suffix.lower() in ('.jpg', '.jpeg'):
            return head.startswith(b'\xff\xd8\xff')
        if real.suffix.lower() == '.webp':
            return head.startswith(b'RIFF') and real.open('rb').read(12)[8:12] == b'WEBP'
        return False
    except OSError:
        return False


if __name__ == '__main__':
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else 'stickers'
    lib = load_library(target)
    total = sum(len(v) for v in lib.values())
    gifs = sum(1 for v in lib.values() for p in v if p.suffix.lower() == '.gif')
    print(f'图库: {target}')
    print(f'分类 {len(lib)} 个 / 共 {total} 张（动图 {gifs} / 静态 {total - gifs}）')
    for cat in categories(lib):
        n = len(lib[cat]); g = sum(1 for p in lib[cat] if p.suffix.lower() == '.gif')
        print(f'  {cat:<8} {n:>3} 张（动图 {g}）')
    for probe in ['发个表情包', '来张动图', '不要发表情', '今天好累', '高兴', '摸鱼']:
        print(f'  {probe!r:<14} 索要={is_request(probe)} 严肃={is_serious(probe)} 动图={wants_animated(probe)}')
