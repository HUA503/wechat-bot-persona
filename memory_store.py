"""Small, explicit, per-sender persistent memory for the WeChat bot."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path


MAX_MEMORIES = 30
MAX_MEMORY_CHARS = 200
_SENSITIVE = re.compile(
    r"(?i)(password|passwd|secret|token|api[ _-]?key|access[ _-]?key|"
    r"密码|口令|密钥|验证码|身份证|银行卡|信用卡|私钥)"
)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS memories (
                group_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, sender_id, fact)
            )""")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def validate(fact: str):
        fact = " ".join(str(fact).split())
        if not fact:
            return None, "内容不能为空。"
        if len(fact) > MAX_MEMORY_CHARS:
            return None, f"每条记忆最多 {MAX_MEMORY_CHARS} 个字。"
        if _SENSITIVE.search(fact):
            return None, "这条内容看起来像敏感信息，我不会保存。"
        return fact, None

    def add(self, group_id: str, sender_id: str, fact: str):
        fact, error = self.validate(fact)
        if error:
            return False, error
        with closing(self._connect()) as conn, conn:
            existing = conn.execute(
                "SELECT 1 FROM memories WHERE group_id=? AND sender_id=? AND fact=?",
                (group_id, sender_id, fact),
            ).fetchone()
            if existing:
                return True, "这条我已经记住了。"
            count = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE group_id=? AND sender_id=?",
                (group_id, sender_id),
            ).fetchone()[0]
            if count >= MAX_MEMORIES:
                return False, f"你的记忆已达 {MAX_MEMORIES} 条上限，请先忘记一些。"
            cur = conn.execute(
                "INSERT OR IGNORE INTO memories(group_id,sender_id,fact) VALUES(?,?,?)",
                (group_id, sender_id, fact),
            )
            return (True, "记住啦。") if cur.rowcount else (False, "保存失败，请稍后再试。")

    def list(self, group_id: str, sender_id: str):
        with closing(self._connect()) as conn, conn:
            return [row[0] for row in conn.execute(
                "SELECT fact FROM memories WHERE group_id=? AND sender_id=? ORDER BY created_at, fact",
                (group_id, sender_id),
            )]

    def forget(self, group_id: str, sender_id: str, fact: str = ""):
        with closing(self._connect()) as conn, conn:
            if not fact:
                cur = conn.execute("DELETE FROM memories WHERE group_id=? AND sender_id=?", (group_id, sender_id))
            else:
                cur = conn.execute("DELETE FROM memories WHERE group_id=? AND sender_id=? AND fact=?", (group_id, sender_id, " ".join(fact.split())))
            return cur.rowcount
