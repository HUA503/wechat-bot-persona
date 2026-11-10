"""收到表情包的识图预处理。

借鉴 whale_girl/bridge/emoji_vision.py：消息里的 <emoji md5=... cdnurl=...>
只是元数据，原图要从微信 CDN 回源，用 md5 严格校验后才能交给模型。
本地 Emoticon 缓存在微信 4.x 是加密的（PIL 打不开、md5 与文件名不符），
因此默认走网络回源；取不到时返回 None，调用方应保持沉默。

安全约束（与 WhaleGirl 一致）：
  * 只请求 .tc.qq.com 的 http/https，拒绝内嵌账号密码与非标准端口
  * 重定向后重新校验域名，全程限 8 MiB
  * 下载内容必须 md5 == 消息里的 md5，或 AES 解密后 md5 相等才采纳
  * PIL 限制总像素/帧数，动图抽首/中/尾帧拼成一张 PNG
"""
from __future__ import annotations

import base64
import hashlib
import html
import io
import re
from urllib.parse import urljoin, urlparse

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_FRAMES = 500
TRUST_SUFFIX = '.tc.qq.com'

SYSTEM_HINT = (
    '\n\n【识图】这一轮用户发来的是表情包图片，不是文字。'
    '请先看清图里的画面、文字和情绪，再以你的人设用一两句自然回应；'
    '不要描述图片、不要说「我看到一张图」、不要复述标记。'
)

PHOTO_HINT = (
    '\n\n【识图】这一轮用户发来的是一张照片/图片，不是文字。'
    '请先看清画面内容和图里的文字，再以你的人设用一两句自然回应；'
    '不要逐字描述图片、不要说「我看到一张图」。'
)

IMAGE_MAGICS = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a', b'RIFF')


def photo_bytes(db, media, chat_id: str, local_id) -> bytes | None:
    """从微信本地 .dat 解出收到的照片字节。只读本机、不碰界面、不联网。

    实测要点：
      * `MediaDownloader.decrypt_image(path)` **不要传 aes_key/xor_key**，
        让它自己推导（本机 xor_key=119）；手工传 0 会毁掉大半像素。
      * 本机只有 `_t.dat` 缩略图档能完整解码（210x210 / 210x467，够识图）；
        `.dat` / `_h.dat` 常常解不出来，所以按 thumb -> mid -> original 顺序试。
      * 每档都用 PIL 完整 load() 验证，避免把「灰色块」的残缺图喂给模型。
    """
    from PIL import Image
    try:
        row = db.get_message_row(chat_id, int(local_id), local_type=3)
        if not row or row.get('local_type') != 3:
            return None
        digest = media._img_md5(row)
        if not digest:
            return None
        files = media._image_files(chat_id, digest)
        for tier in ('thumb', 'mid', 'original'):
            pick = files.get(tier)
            if not pick:
                continue
            try:
                data = media.decrypt_image(pick[0])
                if not data or len(data) > MAX_BYTES or not data.startswith(IMAGE_MAGICS):
                    continue
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                return data
            except Exception:
                continue
    except Exception:
        return None
    return None


def attributes(content: str) -> dict:
    """解析 <emoji ...> 的属性。"""
    tag = re.search(r'<emoji\b([^>]*)>', str(content or ''), re.I)
    if not tag:
        return {}
    return {m.group(1).lower(): html.unescape(m.group(3)) for m in re.finditer(
        r'([a-zA-Z]\w*)\s*=\s*(["\'])(.*?)\2', tag.group(1), re.S)}


def is_emoji(content: str) -> bool:
    return bool(re.search(r'<emoji\b', str(content or ''), re.I))


def trusted_url(url: str) -> bool:
    try:
        v = urlparse(url)
        return (v.scheme in ('http', 'https') and not v.username and not v.password
                and v.port in (None, 80, 443)
                and (v.hostname or '').lower().endswith(TRUST_SUFFIX))
    except ValueError:
        return False


def fetch(url: str, timeout=(5, 10)) -> bytes | None:
    """只从受信任域名下载，重定向也必须重新通过校验。"""
    import requests
    if not trusted_url(url):
        return None
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(3):
            if not trusted_url(url):
                return None
            try:
                with session.get(url, stream=True, allow_redirects=False,
                                 timeout=timeout) as resp:
                    if resp.is_redirect:
                        url = urljoin(url, resp.headers.get('Location', ''))
                        continue
                    resp.raise_for_status()
                    buf = bytearray()
                    for chunk in resp.iter_content(65536):
                        buf.extend(chunk)
                        if len(buf) > MAX_BYTES:
                            return None
                    return bytes(buf)
            except Exception:
                return None
    return None


def _aes_variants(data: bytes, key_hex: str):
    """WhaleGirl 的做法：CDN 可能是加密副本，尝试几种 AES 模式再验 md5。"""
    if not re.fullmatch(r'[0-9a-fA-F]{32}', key_hex or '') or len(data) % 16:
        return
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = bytes.fromhex(key_hex)
        for mode in (modes.CBC(bytes(16)), modes.ECB(), modes.CBC(key)):
            dec = Cipher(algorithms.AES(key), mode).decryptor()
            plain = dec.update(data) + dec.finalize()
            if plain and 0 < plain[-1] <= 16 and plain.endswith(bytes([plain[-1]]) * plain[-1]):
                plain = plain[:-plain[-1]]
            yield plain
    except Exception:
        return


def _accepted(data: bytes, digest: str, key_hex: str) -> bytes | None:
    if not data or len(data) > MAX_BYTES:
        return None
    if hashlib.md5(data).hexdigest() == digest:
        return data
    for plain in _aes_variants(data, key_hex):
        if hashlib.md5(plain).hexdigest() == digest:
            return plain
    return None


def to_preview(data: bytes) -> tuple[bytes, int] | None:
    """解码图片，动图抽首/中/尾帧横向拼成一张 RGB PNG。

    微信本地 .dat 解出的 JPEG 尾部常有几字节缺失（V2 尾部 XOR 未完全还原），
    因此对截断图像宽容处理：能读到像素即可用于识图，不要求完整校验。
    """
    from PIL import Image, ImageFile
    old_flag = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                return None
            count = min(getattr(im, 'n_frames', 1), MAX_FRAMES)
            frames = []
            for idx in sorted({0, count // 2, count - 1}):
                im.seek(idx)
                f = im.convert('RGBA')
                f.thumbnail((768, 768) if count > 1 else (1600, 1600))
                tile = Image.new('RGB', f.size, 'white')
                tile.paste(f, mask=f.getchannel('A'))
                frames.append(tile)
            out = Image.new('RGB', (sum(f.width for f in frames) + 8 * (len(frames) - 1),
                                    max(f.height for f in frames)), 'white')
            x = 0
            for f in frames:
                out.paste(f, (x, 0))
                x += f.width + 8
            buf = io.BytesIO()
            out.save(buf, format='PNG')
            return (buf.getvalue(), len(frames)) if buf.tell() <= MAX_BYTES else None
    except Exception:
        return None
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = old_flag


def load(content: str, allow_network: bool = True) -> tuple[bytes, int] | None:
    """消息 XML -> (预览 PNG 字节, 抽帧数)。取不到返回 None。"""
    attrs = attributes(content)
    digest = (attrs.get('md5') or '').lower()
    if not re.fullmatch(r'[0-9a-f]{32}', digest):
        return None
    if not allow_network:
        return None
    key_hex = attrs.get('aeskey') or ''
    for name in ('cdnurl', 'encrypturl'):
        url = attrs.get(name) or ''
        if not url or not trusted_url(url):
            continue
        raw = fetch(url)
        if not raw:
            continue
        data = _accepted(raw, digest, key_hex)
        if data:
            preview = to_preview(data)
            if preview:
                return preview
    return None


def data_url(png: bytes) -> str:
    return 'data:image/png;base64,' + base64.b64encode(png).decode('ascii')
