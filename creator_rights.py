"""Local publishing grants and creator attribution for TikTok clips."""
import re


def permitted(*, monetize=False):
    terms = "AND g.monetize=1" if monetize else ""
    return f"""EXISTS (SELECT 1 FROM creator_permissions g WHERE g.revoked IS NULL
        AND (g.expires='' OR g.expires>=date('now','localtime')) {terms}
        AND ((g.scope='clip' AND g.subject=c.id)
          OR (g.scope='creator' AND g.subject=lower(trim(c.streamer)))))"""


def credit(conn, clip_id):
    row = conn.execute(f"""SELECT g.credit FROM clips c JOIN creator_permissions g
        ON ((g.scope='clip' AND g.subject=c.id)
            OR (g.scope='creator' AND g.subject=lower(trim(c.streamer))))
        WHERE c.id=? AND g.revoked IS NULL AND (g.expires='' OR g.expires>=date('now','localtime'))
        ORDER BY g.scope='clip' DESC,g.id DESC LIMIT 1""", (clip_id,)).fetchone()
    return row["credit"] if row else ""


def attributed(text, name):
    clean = re.sub(r"(?im)^credit:[^\r\n]*\r?\n?", "", text).rstrip()
    return f"{clean}\nCredit: {name}" if name else clean
