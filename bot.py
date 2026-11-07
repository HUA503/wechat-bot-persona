import json
import re
import sys
import time
import ctypes
from collections import deque
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent

def send_background(db, group_id, group_name, text):
    import win32gui, win32con, win32api, win32clipboard, win32process
    from wechatauto.guia import WeChatGUI
    gui = WeChatGUI()
    hwnd = gui.main_hwnd
    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError('?????????')
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
    if w < 900 or h < 650:
        raise RuntimeError('?????????????')
    def click(rx, ry):
        win32api.SetCursorPos((l+int(w*rx), t+int(h*ry)))
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
    raise RuntimeError('??????????????????')

def get_key():
    import win32crypt
    secret = ROOT / 'secret.txt'
    if not secret.exists():
        raise RuntimeError('缺少 secret.txt，请运行 设置密钥.ps1。')
    encrypted = bytes.fromhex(secret.read_text(encoding='ascii').strip())
    return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode('utf-16-le')

def main():
    import psutil
    from wechatauto import WeChat, WeChatDB
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    group, name = config['group_name'].strip(), config['bot_name'].strip()
    if not group or (config['trigger'] == 'mention' and not name):
        raise RuntimeError('请先在 config.json 填写群名和机器人群昵称。')
    key = get_key()
    history = deque(maxlen=max(0, int(config.get('history_messages', 12))))
    accounts = set()
    for process in psutil.process_iter(['name']):
        if (process.info['name'] or '').lower() != 'weixin.exe':
            continue
        try:
            for item in process.open_files():
                path = Path(item.path)
                for parent in path.parents:
                    if parent.name.lower() == 'db_storage':
                        accounts.add(parent.parent)
                        break
        except (psutil.Error, OSError):
            continue
    if len(accounts) != 1:
        # Weixin.exe has several helper processes, and Windows may deny
        # open_files() for them even when the desktop session is logged in.
        # Fall back to the active user's default WeChat data folders.
        accounts.clear()
        for base in (Path.home() / 'Documents' / 'xwechat_files',
                     Path.home() / 'Documents' / 'WeChat Files'):
            if base.is_dir():
                accounts.update(p.parent for p in base.glob('*/db_storage') if p.is_dir())
    if len(accounts) != 1:
        raise RuntimeError('无法确定唯一的已登录微信账号，请只登录一个微信账号后重试。')
    account = accounts.pop()
    db = WeChatDB(db_dir=str(account.parent), account=account.name,
                  workdir=str(ROOT / 'cache'))
    resolved = db.username_by_nickname(group)
    if not resolved or not resolved.endswith('@chatroom'):
        raise RuntimeError('没有找到指定群聊，请检查群名或先在微信中打开该群。')
    wc = WeChat(db=db)
    started_at = int(time.time())
    self_id = db.get_self_info()['username']

    def on_msg(msg, chat):
        sender = str(getattr(msg, 'sender_wxid', '') or '')
        if (getattr(msg, 'attr', '') != 'friend' or not sender or sender == self_id or
            getattr(msg, 'type', '') != 'text' or int(getattr(msg, 'create_time', 0) or 0) <= started_at):
            return
        content = str(getattr(msg, 'content', '')).strip()
        if not content:
            return
        trigger = config.get('trigger', 'mention')
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
        try:
            persona = (ROOT / 'persona.txt').read_text(encoding='utf-8')
            messages = [{'role': 'system', 'content': persona}, *history,
                        {'role': 'user', 'content': content}]
            resp = requests.post(config['base_url'].rstrip('/') + '/chat/completions',
                                 headers={'Authorization': 'Bearer ' + key},
                                 json={'model': config['model'], 'messages': messages}, timeout=90)
            resp.raise_for_status()
            answer = resp.json()['choices'][0]['message']['content'].strip()
            if not answer:
                raise ValueError('模型返回空内容')
            send_background(db, resolved, group, answer)
            history.extend([{'role': 'user', 'content': content}, {'role': 'assistant', 'content': answer}])
            print('已处理一条群消息。', flush=True)
        except Exception as exc:
            print('处理消息失败：' + str(exc), file=sys.stderr, flush=True)

    result = wc.AddListenChat(nickname=group, callback=on_msg)
    if isinstance(result, dict) and not result:
        raise RuntimeError('监听注册失败：' + str(result))
    print('监听注册结果：', result, flush=True)
    print('正在监听群聊，按 Ctrl+C 退出。', flush=True)
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
        print('启动失败：' + str(exc), file=sys.stderr)
        sys.exit(1)

