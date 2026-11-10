import json
import os
import re
import sys
import time
import ctypes
from collections import deque
from pathlib import Path

import requests
import random

import sticker  # 本地表情包模块（[[sticker:分类]] 机制）
import vision   # 收到表情包的识图预处理（CDN 回源 -> data URL）
import voice    # 真·语音气泡（复用 WhaleGirl 虚拟声卡录制链路）

ROOT = Path(__file__).resolve().parent
ACCOUNT_CACHE = ROOT / '.account_cache.json'
_GUI_CACHE = {}          # 预热好的 WeChatGUI 实例，供发送复用
_CHANCE_CACHE = {'mtime': None, 'base': 0.25, 'by_chat': {}, 'loaded': False}


# Launcher 的 Status 只有 sent / sent_unconfirmed / uncertain / failed 四种。
# 多轮实测（含查微信 DB 核对）后的结论：
#   sent_unconfirmed              → 消息其实已发出，只是拿不到微信回执；
#   uncertain 且 Error 为空        → 同上，按已发出处理；
#   uncertain/failed 且 Error 提到「无法确认会话 / 快捷键不一致 / 窗口或会话已变化」
#                                 → Launcher 在开始录音之前就退出了，确实没发，可安全重试。
# 注意：不能靠查 DB 做实时判定 —— 微信走 WAL，新语音要十几秒才进主库，
# 实测发送后 8 秒内查不到、过一会儿又有；据此重试会造成重复消息（已验证会发两条）。
VOICE_NOT_SENT_MARKS = (
    '无法确认目标账号或会话',      # prepare() 没拿到会话，未开始录音
    '无法确认当前微信会话',        # Launcher 自查会话失败，未开始录音
    '快捷键与配置不一致',          # 录音前校验就失败
    '窗口或会话已变化',            # 录音被取消
    '语音发送未启用',
    '语音接口连续异常',
)


def voice_really_failed(detail: dict) -> bool:
    """能否判定这次语音「确实没发出去」。没有错误信息就不算失败。"""
    err = str((detail or {}).get('Error') or '')
    if not err:
        return False
    return any(mark in err for mark in VOICE_NOT_SENT_MARKS)


def send_voice_verified(sender, attempts: int = 2, log=print):
    """发送语音；只在「确实没发」时才重试，避免重复消息。

    sender() 返回 (ok, detail)。sent_unconfirmed / uncertain 一律按成功处理，
    它们只代表 Launcher 拿不到微信回执，消息本身已经发出去了。
    """
    detail = {}
    for attempt in range(1, max(1, attempts) + 1):
        try:
            ok, detail = sender()
        except Exception as exc:
            ok, detail = False, {'Status': 'failed', 'Error': f'{type(exc).__name__}: {exc}'}
        detail = dict(detail or {})
        if not voice_really_failed(detail):
            landed = bool(ok) or detail.get('Status') in ('sent', 'sent_unconfirmed', 'uncertain')
            detail['Landed'] = landed
            return landed, detail
        if attempt < attempts:
            log(f'语音第 {attempt} 次确实未发出（{detail.get("Status")}/'
                f'{detail.get("Error", "")}），重试')
            time.sleep(1.0)
    detail['Landed'] = False
    return False, detail


def current_chance(chat_name: str = '', default: float = 0.25) -> float:
    """读取「主动发图概率」：改 config.json 立即生效，无需重启。

    按文件 mtime 惰性重载，正常每轮只做一次 stat。支持 sticker.chance_by_chat
    给单个会话（群名 / 私聊备注）单独设值；未列出的会话用全局 sticker.chance。
    配置临时写坏时保留上次值，不会每轮刷错误日志。
    """
    cfg_path = ROOT / 'config.json'
    try:
        mt = cfg_path.stat().st_mtime
    except OSError:
        return _CHANCE_CACHE['base'] if _CHANCE_CACHE['loaded'] else default
    if mt != _CHANCE_CACHE['mtime']:
        _CHANCE_CACHE['mtime'] = mt
        try:
            sc = json.loads(cfg_path.read_text(encoding='utf-8')).get('sticker') or {}
            base = max(0.0, min(1.0, float(sc.get('chance', default))))
            by = {}
            for k, v in (sc.get('chance_by_chat') or {}).items():
                by[str(k).strip()] = max(0.0, min(1.0, float(v)))
            _CHANCE_CACHE.update(base=base, by_chat=by, loaded=True)
            extra = ('，会话覆盖：' + '、'.join(f'{k}={v:.0%}' for k, v in by.items())) if by else ''
            print(f'主动发图概率：全局 {base:.0%}{extra}（改 config 立即生效）', flush=True)
        except Exception as exc:
            print(f'[警告] 发图概率读取失败，沿用上次值：{exc}',
                  file=sys.stderr, flush=True)
    by = _CHANCE_CACHE['by_chat']
    return by.get(chat_name, _CHANCE_CACHE['base'] if _CHANCE_CACHE['loaded'] else default)


def chat_completion(config, key, messages, timeout=90, attempts=3, log=print):
    """调模型接口，对 429/5xx/网络抖动做指数退避重试。

    限流时优先遵守 Retry-After，其次 1.5s→3s 退避；非重试型错误（401/400/内容
    为空等）立即抛出，避免把配置错误也拖成 3 次、白等十几秒。
    """
    url = config['base_url'].rstrip('/') + '/chat/completions'
    payload = {'model': config['model'], 'messages': messages}
    retryable = (429, 500, 502, 503, 504)
    headers = {'Authorization': 'Bearer ' + key}
    last = None
    for i in range(1, attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            # 只有连接失败/超时这类传输层异常才值得重试
            last = exc
            if i >= attempts:
                raise
            delay = 1.5 * (2 ** (i - 1))
            log(f'模型接口异常（{i}/{attempts}）{type(exc).__name__}，{delay:.1f}s 后重试',
                file=sys.stderr, flush=True)
            time.sleep(delay)
            continue
        if resp.status_code in retryable and i < attempts:
            delay = 0.0
            try:
                delay = float(resp.headers.get('Retry-After') or 0)
            except (TypeError, ValueError):
                delay = 0.0
            delay = min(10.0, delay) if delay > 0 else 1.5 * (2 ** (i - 1))
            log(f'模型接口 {resp.status_code}（{i}/{attempts}），{delay:.1f}s 后重试', flush=True)
            last = f'HTTP {resp.status_code}'
            time.sleep(delay)
            continue
        # 4xx（密钥/参数/额度）不属于抖动，立即抛出，别把配置错误拖成多次等待
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content'].strip()
    raise RuntimeError(f'模型接口重试 {attempts} 次仍失败：{last}')


def _account_health(acc: Path) -> float:
    """账号活跃度（最新消息库 mtime）；目录无效返回 0。"""
    try:
        msg = acc / 'db_storage' / 'message' / 'message_0.db'
        if msg.is_file():
            return msg.stat().st_mtime
        ds = acc / 'db_storage'
        return ds.stat().st_mtime if ds.is_dir() else 0.0
    except OSError:
        return 0.0


def _account_candidates():
    """候选账号：上次成功的排第一，其余按活跃度降序。不枚举进程句柄（约 1ms）。"""
    cached = None
    try:
        if ACCOUNT_CACHE.is_file():
            p = Path(json.loads(ACCOUNT_CACHE.read_text(encoding='utf-8'))['account'])
            if _account_health(p):
                cached = p
    except Exception:
        pass
    found = []
    for base in (Path.home() / 'xwechat_files',
                 Path.home() / 'Documents' / 'xwechat_files',
                 Path.home() / 'Documents' / 'WeChat Files'):
        if base.is_dir():
            found.extend(d.parent for d in base.glob('*/db_storage') if d.is_dir())
    found.sort(key=_account_health, reverse=True)
    return ([cached] if cached else []) + [p for p in found if p != cached]


def _scan_accounts_by_handles():
    """兜底：枚举微信进程句柄精确定位（慢，约 1 秒），仅在快速路径全部失败时使用。"""
    import psutil
    accounts = set()
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() != 'weixin.exe':
            continue
        try:
            for item in process.open_files():
                for parent in Path(item.path).parents:
                    if parent.name.lower() == 'db_storage':
                        accounts.add(parent.parent)
                        break
        except (psutil.Error, OSError):
            continue
    return accounts


def locate_account(group: str, fast: bool = True):
    """定位账号并建立 DB。返回 (db, resolved_group_wxid, account_dir)。

    快速路径用「缓存 + 目录活跃度」猜候选（1~5ms），每个候选都用「能否解析出
    目标群」校验，猜错就试下一个；全部失败才回退到精确句柄扫描。因此省掉约
    1 秒的进程/句柄枚举，同时不会读错账号。
    """
    from wechatauto import WeChatDB
    tried, errors = set(), []
    saw_valid_db = [False]

    def attempt(acc):
        if not acc or acc in tried or not _account_health(acc):
            return None
        tried.add(acc)
        try:
            db = None
            # WinError 5：另一个进程（状态检查脚本 / 诊断脚本）正在同一 cache 目录
            # 做 DB 快照替换，短暂重试即可，不必整体失败。
            for k in range(3):
                try:
                    db = WeChatDB(db_dir=str(acc.parent), account=acc.name,
                                  workdir=str(ROOT / 'cache'))
                    break
                except OSError as exc:
                    if getattr(exc, 'winerror', None) != 5 or k == 2:
                        raise
                    time.sleep(0.4)
            saw_valid_db[0] = True
            resolved = db.username_by_nickname(group)
            if resolved and resolved.endswith('@chatroom'):
                try:
                    ACCOUNT_CACHE.write_text(
                        json.dumps({'account': str(acc)}, ensure_ascii=False),
                        encoding='utf-8')
                except OSError:
                    pass
                return db, resolved, acc
        except Exception as exc:
            errors.append(f'{acc.name}: {type(exc).__name__}')
        return None

    if fast:
        for acc in _account_candidates()[:4]:
            hit = attempt(acc)
            if hit:
                return hit
    for acc in _scan_accounts_by_handles():
        hit = attempt(acc)
        if hit:
            return hit
    if saw_valid_db[0]:
        raise RuntimeError(f'没有找到指定群聊「{group}」，请检查群名或先在微信中打开该群。')
    raise RuntimeError('无法确定唯一的已登录微信账号，请只登录一个微信账号后重试。'
                       + ('（试探：' + '；'.join(errors[:3]) + '）' if errors else ''))


def warmup_gui():
    """后台预热 GUI/UIA，让第一条消息不必临场热激活与布局校准。"""
    try:
        from wechatauto.guia import WeChatGUI
        gui = WeChatGUI()
        _GUI_CACHE['gui'] = gui
        try:
            gui._get_uia()      # 惰性 UIA 热激活，失败会自动降级 OCR，不影响功能
        except Exception:
            pass
        print('GUI/UIA 预热完成。', flush=True)
    except Exception as exc:
        print('[警告] GUI 预热失败（不影响运行）：' + str(exc),
              file=sys.stderr, flush=True)


def send_background(db, group_id, group_name, text):
    import win32gui, win32con, win32api, win32clipboard, win32process
    from wechatauto.guia import WeChatGUI
    gui = _GUI_CACHE.get('gui') or WeChatGUI()
    hwnd = gui.main_hwnd
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError('微信主窗口不可用，请确认微信已登录且窗口正常显示')
    rows = db.get_messages(group_id, limit=30)
    watermark = {(int(r.get('sort_seq') or 0), int(r.get('local_id') or 0)) for r in rows}
    highest = max((int(r.get('sort_seq') or 0) for r in rows), default=0)
    self_id = db.get_self_info()['username']
    # This GUI automation currently selects the first chat in the session list.
    # Keep the target group pinned at the top before enabling automatic replies.
    fg = win32gui.GetForegroundWindow()
    cur_tid = win32api.GetCurrentThreadId()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    target_tid = win32process.GetWindowThreadProcessId(hwnd)[0]
    attached = []
    for tid in {fg_tid, target_tid} - {0, cur_tid}:
        if win32process.AttachThreadInput(cur_tid, tid, True):
            attached.append(tid)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        win32gui.SetActiveWindow(hwnd)
    finally:
        for tid in attached:
            win32process.AttachThreadInput(cur_tid, tid, False)
    time.sleep(.5)
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r-l, b-t
    sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    # Scale the minimum window size with the display instead of a fixed 900x650.
    # Keep an absolute floor so tiny displays still get a usable layout.
    min_w = max(600, int(sw * 0.45))
    min_h = max(400, int(sh * 0.55))
    if w < min_w or h < min_h:
        raise RuntimeError(
            f'微信窗口过小（当前 {w}x{h}），在 {sw}x{sh} 屏幕上至少需要 {min_w}x{min_h}，请放大窗口或直接最大化')
    def click(rx, ry):
        x, y = l+int(w*rx), t+int(h*ry)
        if not (0 <= x < sw and 0 <= y < sh):
            raise RuntimeError(
                f'点击坐标 ({x},{y}) 超出屏幕 {sw}x{sh}，请把微信窗口完整移入屏幕内（或用最大化）')
        win32api.SetCursorPos((x, y))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(.08)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    click(.18, .17)
    time.sleep(.5)
    click(.67, .82)
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    for vk, flags in ((win32con.VK_CONTROL,0),(ord('V'),0),(ord('V'),win32con.KEYEVENTF_KEYUP),(win32con.VK_CONTROL,win32con.KEYEVENTF_KEYUP)):
        win32api.keybd_event(vk,0,flags,0)
    time.sleep(.4)
    win32api.keybd_event(win32con.VK_RETURN,0,0,0)
    win32api.keybd_event(win32con.VK_RETURN,0,win32con.KEYEVENTF_KEYUP,0)
    for _ in range(20):
        for row in db.get_messages(group_id, limit=30):
            seq = int(row.get('sort_seq') or 0)
            identity = (seq, int(row.get('local_id') or 0))
            if seq >= highest and identity not in watermark and row.get('sender_username') == self_id and row.get('content') == text:
                return
        time.sleep(1)
    raise RuntimeError('发送超时：消息没有出现在微信聊天记录里，请确认目标群在会话列表第一位且窗口未被遮挡')

def notify(title: str, text: str, timeout_ms: int = 10000, wait: bool = False):
    """弹出启动结果提示。成功时用独立线程，绝不阻塞监听；失败时同步显示后再退出。"""
    def _show():
        user32 = ctypes.windll.user32
        flags = 0x40 | 0x40000  # MB_ICONINFORMATION | MB_TOPMOST
        try:
            fn = user32.MessageBoxTimeoutW  # 自动超时关闭，无需用户点确定
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                           ctypes.c_uint, ctypes.c_ushort, ctypes.c_uint]
            fn.restype = ctypes.c_int
            fn(None, text, title, flags, 0, timeout_ms)
        except Exception:
            try:
                fn2 = user32.MessageBoxW
                fn2.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                ctypes.c_wchar_p, ctypes.c_uint]
                fn2.restype = ctypes.c_int
                fn2(None, text, title, flags)
            except Exception:
                pass

    if wait:
        _show()
    else:
        import threading
        threading.Thread(target=_show, daemon=True).start()


def get_key():
    import win32crypt
    secret = ROOT / 'secret.txt'
    if not secret.exists():
        raise RuntimeError('缺少 secret.txt，请运行 设置密钥.ps1。')
    encrypted = bytes.fromhex(secret.read_text(encoding='ascii').strip())
    return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode('utf-16-le')

def main():
    from wechatauto import WeChat
    t_boot = time.perf_counter()
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    group, name = config['group_name'].strip(), config['bot_name'].strip()
    if not group or (config['trigger'] == 'mention' and not name):
        raise RuntimeError('请先在 config.json 填写群名和机器人群昵称。')
    key = get_key()
    history_size = max(0, int(config.get('history_messages', 12)))
    histories = {}  # 按会话隔离上下文，避免群聊与不同私聊之间串味
    private_chats = [str(x).strip() for x in config.get('private_chats', []) if str(x).strip()]
    startup_cfg = config.get('startup') or {}
    t_locate = time.perf_counter()
    db, resolved, account = locate_account(
        group, fast=bool(startup_cfg.get('fast_account', True)))
    locate_ms = (time.perf_counter() - t_locate) * 1000
    # 私聊白名单：空列表 = 完全不回复私聊。名字必须是微信备注/昵称的精确匹配。
    private_targets = {}
    for nickname in private_chats:
        wxid = db.username_by_nickname(nickname)
        if not wxid:
            print(f'[警告] 找不到私聊对象（备注/昵称需精确匹配）：{nickname}', file=sys.stderr, flush=True)
            continue
        if wxid.endswith('@chatroom'):
            print(f'[警告] {nickname} 是群聊，请改用 group_name：已跳过', file=sys.stderr, flush=True)
            continue
        private_targets[nickname] = wxid
    # 表情包图库：目录一级子目录即分类名，默认用程序目录下的 stickers
    scfg = config.get('sticker') or {}
    sticker_on = bool(scfg.get('enabled', False))
    sticker_root = str(scfg.get('dir') or (ROOT / 'stickers'))
    sticker_lib = sticker.load_library(sticker_root) if sticker_on else {}
    sticker_chance = max(0.0, min(1.0, float(scfg.get('chance', 0.25))))
    sticker_recent_n = max(1, int(scfg.get('recent', 10)))
    sticker_fast = bool(scfg.get('fast', True))
    sticker_fast_wait = max(0.2, float(scfg.get('fast_wait', 0.8)))
    # 识图：会把收到的表情包图片发给模型服务商，故做成显式开关
    vcfg = config.get('vision') or {}
    vision_on = bool(vcfg.get('enabled', False))
    vision_net = bool(vcfg.get('allow_network', True))
    vision_photos = bool(vcfg.get('photos', True))
    # 微信先写消息库、后落图片 .dat；回调往往早于文件写完，短重试可消除这种竞态
    photo_retries = max(1, int(vcfg.get('photo_retries', 5)))
    photo_retry_wait = max(0.1, float(vcfg.get('photo_retry_wait', 0.5)))
    vision_fail_reply = bool(vcfg.get('reply_on_fail', True))
    media_dl = None
    if vision_on and vision_photos:
        try:
            from wechatauto.media import MediaDownloader
            media_dl = MediaDownloader(db, save_dir=str(ROOT / 'cache' / 'vision'))
        except Exception as exc:
            print(f'[警告] 图片解密器初始化失败，照片识图不可用：{exc}',
                  file=sys.stderr, flush=True)
    if vision_on:
        print(f'识图已启用（表情包回源={vision_net} / 照片={bool(media_dl)}）：'
              '图片会发送给模型服务商', flush=True)
    sticker_recent = {}      # 会话 -> 最近发过的图片路径
    sticker_inflight = set() # 同一会话同时只发一张，避免刷屏
    if sticker_on:
        total = sum(len(v) for v in sticker_lib.values())
        print(f'表情包图库：{len(sticker_lib)} 类 / {total} 张（{sticker_root}）', flush=True)
        if not sticker_lib:
            print('[警告] 表情包目录为空或不存在，本轮不会发图', file=sys.stderr, flush=True)
    wc = WeChat(db=db)
    started_at = int(time.time())
    self_id = db.get_self_info()['username']

    def on_msg(msg, chat):
        chat_name = str(getattr(chat, 'who', '') or '')
        is_private = chat_name in private_targets
        sender = str(getattr(msg, 'sender_wxid', '') or '')
        raw_msg = str(getattr(msg, 'content', '') or '')
        mtype = str(getattr(msg, 'type', '') or '')
        is_sticker_msg = vision_on and vision.is_emoji(raw_msg)
        is_photo_msg = (vision_on and bool(media_dl) and mtype == 'image'
                        and '<img ' in raw_msg)
        is_vision_msg = is_sticker_msg or is_photo_msg
        if (getattr(msg, 'attr', '') != 'friend' or not sender or sender == self_id or
            (mtype != 'text' and not is_vision_msg) or
            int(getattr(msg, 'create_time', 0) or 0) <= started_at):
            return
        sticker_png = None
        if is_sticker_msg:
            # 借鉴 WhaleGirl：读不到图就保持沉默，绝不瞎猜图片内容
            try:
                preview = vision.load(raw_msg, allow_network=vision_net)
            except Exception as exc:
                print('识图回源异常：' + str(exc), file=sys.stderr, flush=True)
                return
            if not preview:
                print('识图跳过：取不到表情包预览，保持沉默', flush=True)
                return
            sticker_png = preview[0]
            content = '[表情包]'
        elif is_photo_msg:
            chat_id = str(getattr(chat, '_wxid', '') or '')
            local_id = getattr(msg, 'local_id', None)
            preview = None
            # 微信 DB 记录通常比 .dat 落盘早几百毫秒到几秒，重试等文件写完
            for attempt in range(photo_retries):
                try:
                    data = vision.photo_bytes(db, media_dl, chat_id, local_id)
                    preview = vision.to_preview(data) if data else None
                except Exception as exc:
                    print('照片解密异常：' + str(exc), file=sys.stderr, flush=True)
                    return
                if preview:
                    if attempt:
                        print(f'照片已读到（第 {attempt + 1} 次尝试，lid={local_id}）。', flush=True)
                    break
                if attempt + 1 < photo_retries:
                    time.sleep(photo_retry_wait)
            if not preview:
                print(f'识图跳过：本地解不出这张照片（已重试 {photo_retries} 次，'
                      f'chat={chat_id} lid={local_id}）', flush=True)
                if vision_fail_reply:
                    # 明确告知失败，避免用户以为机器人「没反应」
                    try:
                        tip = '这张图我这边还没读到，稍等一下再发一次？'
                        if is_private:
                            chat.SendMsg(tip)
                        else:
                            send_background(db, resolved, group, tip)
                        print('已发送识图失败提示。', flush=True)
                    except Exception as exc:
                        print('识图失败提示发送异常：' + str(exc), file=sys.stderr, flush=True)
                return
            sticker_png = preview[0]
            content = '[照片]'
        else:
            content = raw_msg.strip()
            if not content:
                return
        trigger = 'all' if is_vision_msg else config.get('trigger', 'mention')
        if is_private and not is_vision_msg:
            pt = config.get('private_trigger', 'inherit')
            if pt not in ('', 'inherit', None):
                trigger = pt
            if trigger == 'mention':
                trigger = 'all'  # 私聊没有 @ 的意义
        if trigger == 'mention':
            pattern = r'@' + re.escape(name) + r'(?:[\s\u2005]|$)'
            if not re.search(pattern, content):
                return
            content = re.sub(pattern, '', content, count=1).strip()
        elif trigger == 'prefix':
            prefix = config.get('prefix', '小鲸')
            if not content.startswith(prefix):
                return
            content = content[len(prefix):].strip()
        elif trigger != 'all':
            return
        if not content:
            content = '你好'
        # 频率闸门放在 prompt 注入阶段：只有本轮被允许，模型才会知道可以写标记，
        # 因此不会出现「模型以为发了但其实没发」的不一致。明确索要时强制放行。
        requested = False if is_vision_msg else sticker.is_request(content)
        chance = current_chance(chat_name, sticker_chance)   # 热更新，改配置无需重启
        # 语音决策提前到 prompt 阶段：模型必须知道本轮会发语音，否则会顺着旧
        # 上下文说「我发不了语音」，也便于让它写成适合朗读的短句。
        voice_cfg_now = voice.read_config(ROOT / 'config.json')
        use_voice, why_voice = (voice.should_reply_by_voice(content, not is_private, voice_cfg_now)
                                if not is_vision_msg else (False, 'vision_round'))
        allow_sticker = (sticker_on and bool(sticker_lib) and not is_vision_msg
                         and not use_voice
                         and not sticker.is_serious(content)
                         and chat_name not in sticker_inflight
                         and (requested or random.random() < chance))
        try:
            persona = (ROOT / 'persona.txt').read_text(encoding='utf-8')
            if is_vision_msg:
                hint = vision.PHOTO_HINT if is_photo_msg else vision.SYSTEM_HINT
                lead = ('（对方只发来这张照片，没有文字。请先看图再回应）' if is_photo_msg
                        else '（对方只发来这张表情包，没有文字。请先看图再回应）')
                system = persona + hint
                user_msg = {'role': 'user', 'content': [
                    {'type': 'text', 'text': lead},
                    {'type': 'image_url', 'image_url': {'url': vision.data_url(sticker_png)}}]}
            else:
                system = (persona
                          + (voice.VOICE_HINT if use_voice else '')
                          + (sticker.prompt_hint(sticker_lib) if allow_sticker else ''))
                user_msg = {'role': 'user', 'content': content}
            history = histories.setdefault(chat_name, deque(maxlen=history_size))
            messages = [{'role': 'system', 'content': system}, *history, user_msg]
            raw = chat_completion(config, key, messages)
            answer, sticker_cat = sticker.parse(raw) if sticker_on else (raw, None)
            want_pic = allow_sticker and (bool(sticker_cat) or requested)
            if not answer and not want_pic:
                raise ValueError('模型返回空内容')
            # 模型只吐标记时补一句短语：文字会把会话切到目标，
            # 后面的快路径才能安全地复用这个已打开的会话。
            if want_pic and not answer:
                answer = random.choice(sticker.QUICK_LINES)
            # 语音回复：明确索要（或概率命中）时发真·语音气泡。
            # 成功就不再发文字、也不叠加发图，避免重复回复与 GUI 抢焦点。
            voice_used = False
            if answer and not is_vision_msg:
                if use_voice:
                    vtext = answer
                    maxc = max(20, int(voice_cfg_now.get('max_chars', 120) or 120))
                    if len(vtext) > maxc:
                        vtext = vtext[:maxc].rstrip('，,。.！!？?…、 ') + '。'
                    gui = _GUI_CACHE.get('gui')
                    if gui is None:
                        try:
                            from wechatauto.guia import WeChatGUI
                            gui = WeChatGUI()
                        except Exception as exc:
                            print(f'语音窗口不可用：{exc}', file=sys.stderr, flush=True)
                    if gui is not None:
                        ok, detail = send_voice_verified(
                            lambda: voice.send(vtext, chat_name, gui, ROOT / 'config.json',
                                               log=lambda *a: print(*a, flush=True)),
                            log=lambda m: print(m, file=sys.stderr, flush=True))
                        if ok:
                            voice_used = True
                            want_pic = False
                            vlabel = f'私聊 {chat_name}' if is_private else f'群聊 {chat_name}'
                            print(f'已发送语音回复（{vlabel}）：{why_voice} / '
                                  f'{detail.get("Status", "unknown")}', flush=True)
                        else:
                            print(f'语音发送失败，回退文字：{why_voice} / '
                                  f'{detail.get("Error", "")}', file=sys.stderr, flush=True)
            text_sent = False
            if answer and not voice_used:
                if is_private:
                    sent = chat.SendMsg(answer)
                    if not sent:
                        raise RuntimeError(f'私聊发送失败：{sent}')
                    label = f'私聊 {chat_name}'
                else:
                    send_background(db, resolved, group, answer)
                    label = f'群聊 {chat_name}'
                text_sent = True
                if is_vision_msg:
                    kind = '照片' if is_photo_msg else '表情包'
                    print(f'已识别{kind}并回复（{label}）。', flush=True)
                else:
                    print(f'已处理一条{label}消息。', flush=True)
            history.extend([{'role': 'user', 'content': (
                '[收到一张照片]' if is_photo_msg else
                '[收到一张表情包]' if is_sticker_msg else content)},
                            {'role': 'assistant', 'content': answer or '[表情包]'}])
            if want_pic:
                sticker_inflight.add(chat_name)
                try:
                    recent = sticker_recent.setdefault(chat_name, deque(maxlen=sticker_recent_n))
                    animated = sticker.wants_animated(content)
                    hit = sticker.pick(sticker_lib, sticker_cat or '', list(recent), animated=animated)
                    if not hit:
                        raise RuntimeError('图库里没有可用图片')
                    path, cat = hit
                    if not sticker.validate(path, sticker_root):
                        raise RuntimeError('图片校验未通过（格式或路径异常）')
                    route = '标准'
                    if sticker_fast and text_sent:
                        try:
                            sticker.fast_send(path, wait=sticker_fast_wait)
                            route = '快'
                        except Exception as exc:
                            print(f'表情包快路径跳过（{exc}），回退标准发送',
                                  file=sys.stderr, flush=True)
                    if route != '快':
                        sr = chat.SendFiles(str(path))
                        if not sr:
                            raise RuntimeError(f'发送失败：{sr}')
                    recent.append(str(path))
                    history[-1]['content'] = f'{answer}（并发了{cat}表情包）'
                    print(f'已发送表情包（{route}）：{cat}/{path.name} → {chat_name}', flush=True)
                except Exception as exc:
                    print('处理表情包失败：' + str(exc), file=sys.stderr, flush=True)
                finally:
                    sticker_inflight.discard(chat_name)
        except Exception as exc:
            print('处理消息失败：' + str(exc), file=sys.stderr, flush=True)

    result = wc.AddListenChat(nickname=group, callback=on_msg)
    if isinstance(result, dict) and not result:
        raise RuntimeError('群聊监听注册失败：' + str(result))
    print('群聊监听注册结果：', result, flush=True)
    registered = 1
    ok_private = []
    for nickname in private_targets:
        r = wc.AddListenChat(nickname=nickname, callback=on_msg)
        if isinstance(r, dict) and not r:
            print(f'[警告] 私聊「{nickname}」监听注册失败：{r}', file=sys.stderr, flush=True)
        else:
            registered += 1
            ok_private.append(nickname)
            print(f'私聊监听已注册：{nickname}', flush=True)
    print(f'正在监听 {registered} 个会话（群聊 1 / 私聊 {registered - 1}），按 Ctrl+C 退出。', flush=True)
    boot_ms = (time.perf_counter() - t_boot) * 1000
    fast_acc = bool(startup_cfg.get('fast_account', True))
    print(f'启动耗时 {boot_ms:.0f}ms（账号定位/DB快照 {locate_ms:.0f}ms，快速账号={fast_acc}）',
          flush=True)
    current_chance('', sticker_chance)   # 启动时打印一次当前生效的概率
    voice_cfg = voice.read_config(ROOT / 'config.json')
    if voice_cfg.get('enabled'):
        miss = voice.check_dependencies(voice_cfg.get('whalegirl_dir') or voice.DEFAULT_ROOT)
        print('语音依赖：' + ('齐全（虚拟声卡 / TTS / Launcher）' if not miss
                          else '缺失 → ' + '、'.join(miss)), flush=True)
    if startup_cfg.get('warmup_gui', True):
        # 后台预热 GUI/UIA，不阻塞监听与启动弹窗；首条消息不必临场热激活
        import threading
        threading.Thread(target=warmup_gui, daemon=True).start()
    if config.get('notify_on_start', True):
        sessions = [f'群聊 {group}'] + [f'私聊 {n}' for n in ok_private]
        notify('微信机器人 · 启动成功',
               f'正在监听 {registered} 个会话：\n  · ' + '\n  · '.join(sessions) +
               f'\n\nPID：{os.getpid()}\n日志：bot.log / error.log'
               '\n\n（10 秒后自动关闭，不影响后台运行）')
    wc.KeepRunning()

if __name__ == '__main__':
    try:
        import msvcrt
        lock = (ROOT / 'bot.lock').open('a+b')
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('机器人已经运行，请勿重复启动。')
        main()
    except KeyboardInterrupt:
        print('\n已停止。')
    except Exception as exc:
        reason = str(exc)
        print('启动失败：' + reason, file=sys.stderr)
        if '已经运行' in reason:
            notify('微信机器人', '机器人已经在运行，无需重复启动。\n\n查看状态可双击 查看状态.bat', wait=True)
        else:
            notify('微信机器人 · 启动失败',
                   '启动失败：\n' + reason + '\n\n详情见 error.log', wait=True)
        sys.exit(1)

