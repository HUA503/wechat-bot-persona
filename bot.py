import json
import re
import sys
import time
import ctypes
import os
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
        try:
            win32gui.SetForegroundWindow(hwnd)
            win32gui.SetActiveWindow(hwnd)
        except Exception:
            # Windows may block foreground changes from a background process.
            # An Alt tap grants the process a foreground-switch opportunity.
            win32api.keybd_event(win32con.VK_MENU,0,0,0)
            win32api.keybd_event(win32con.VK_MENU,0,win32con.KEYEVENTF_KEYUP,0)
            try:
                win32gui.SetForegroundWindow(hwnd)
                win32gui.SetActiveWindow(hwnd)
            except Exception:
                # The coordinate click below can still activate the visible window.
                pass
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
    # Do not append a reply to any text already in the compose box.
    win32api.keybd_event(win32con.VK_CONTROL,0,0,0)
    win32api.keybd_event(ord('A'),0,0,0)
    win32api.keybd_event(ord('A'),0,win32con.KEYEVENTF_KEYUP,0)
    win32api.keybd_event(win32con.VK_CONTROL,0,win32con.KEYEVENTF_KEYUP,0)
    win32api.keybd_event(win32con.VK_BACK,0,0,0)
    win32api.keybd_event(win32con.VK_BACK,0,win32con.KEYEVENTF_KEYUP,0)
    time.sleep(.1)
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
        # Cloudflare Access may be the only auth layer for a private Ollama endpoint.
        return ''
    encrypted = bytes.fromhex(secret.read_text(encoding='ascii').strip())
    return win32crypt.CryptUnprotectData(encrypted, None, None, None, 0)[1].decode('utf-16-le')

def main():
    import psutil
    from wechatauto import WeChat, WeChatDB
    from memory_store import MemoryStore
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
    memory = MemoryStore(ROOT / 'cache' / 'memory.sqlite3')
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
        # Long-term memory is explicit and scoped to this sender within this group.
        # Do not send memory-management commands to the model or include them in chat history.
        command = re.match(r'^(?:记住[:：]|/记住\s+)(.*)$', content, flags=re.S)
        forget = re.match(r'^(?:忘记[:：]\s*(.*)|/忘记(?:\s+(.*))?)$', content, flags=re.S)
        if command:
            ok, reply = memory.add(resolved, sender, command.group(1))
            if ok and reply == '记住啦。':
                reply = '记住啦，会按你在本群的微信账号单独记录。'
            send_background(db, resolved, group, reply)
            return
        if forget:
            target = (forget.group(1) or forget.group(2) or '').strip()
            if target in ('全部', '所有', 'all'):
                count = memory.forget(resolved, sender)
                reply = f'已经忘掉你的 {count} 条记忆。'
            elif target:
                count = memory.forget(resolved, sender, target)
                reply = '这条记忆已经删掉啦。' if count else '没有找到完全相同的记忆。'
            else:
                reply = '告诉我具体要忘记哪条；要清空请发“忘记：全部”。'
            send_background(db, resolved, group, reply)
            return
        if content in ('记忆', '/记忆', '我记得什么'):
            facts = memory.list(resolved, sender)
            reply = ('我目前记着这些：\n' + '\n'.join(f'• {item}' for item in facts)
                     if facts else '我还没有记住你的个人信息。可以发“记住：内容”，让我记住某件事。')
            if facts:
                reply += '\n提醒：群聊回复对群成员可见，请别保存不适合公开的内容。'
            send_background(db, resolved, group, reply[:1800])
            return
        if not content:
            content = '你好'
        try:
            persona = (ROOT / 'persona.txt').read_text(encoding='utf-8')
            facts = memory.list(resolved, sender)
            if facts:
                persona += '\n\n【本群当前发言者的明确记忆】\n' + '\n'.join(f'- {item}' for item in facts)
                persona += '\n这些内容仅属于当前发言者。不要推断或泄露给其他群成员；仅在相关时自然使用。'
            messages = [{'role': 'system', 'content': persona}, *history,
                        {'role': 'user', 'content': content}]
            headers = {}
            if key:
                headers['Authorization'] = 'Bearer ' + key
            access_id = os.environ.get('OLLAMA_CF_ACCESS_CLIENT_ID', '').strip()
            access_secret = os.environ.get('OLLAMA_CF_ACCESS_CLIENT_SECRET', '').strip()
            if bool(access_id) != bool(access_secret):
                raise RuntimeError('Cloudflare Access 配置不完整。')
            if access_id:
                headers['CF-Access-Client-Id'] = access_id
                headers['CF-Access-Client-Secret'] = access_secret
            if not headers:
                raise RuntimeError('未配置模型 API 凭据。')
            payload = {'model': config['model'], 'messages': messages}
            if config.get('max_tokens'):
                payload['max_tokens'] = int(config['max_tokens'])
            resp = requests.post(config['base_url'].rstrip('/') + '/chat/completions',
                                 headers=headers, json=payload,
                                 timeout=int(config.get('request_timeout', 45)))
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

