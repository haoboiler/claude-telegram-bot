import os
import re


# Image extensions that can be sent as photos (Telegram supports these natively)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# File extensions worth auto-sending (common output formats)
SENDABLE_EXTS = IMAGE_EXTS | {
    ".pdf",
    ".csv",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".html",
    ".svg",
    ".mp4",
    ".mp3",
    ".zip",
    ".tar",
    ".gz",
    ".txt",
    ".md",
    ".json",
}

# Telegram file size limit (50MB for bots)
TG_FILE_SIZE_LIMIT = 50 * 1024 * 1024

# Regex to find file paths in response text
# Matches absolute paths and paths starting with ./
FILE_PATH_RE = re.compile(
    r'(?:^|[\s`\'"])(/[\w./_-]+\.[\w]+|\.\/[\w./_-]+\.[\w]+)',
)


def split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split long messages for Telegram's 4096 char limit."""
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break

        split_pos = text.rfind("\n", 0, max_len)
        if split_pos == -1:
            split_pos = text.rfind(" ", 0, max_len)
        if split_pos == -1:
            split_pos = max_len

        parts.append(text[:split_pos])
        text = text[split_pos:].lstrip("\n")

    return parts


def extract_sendable_files(
    text: str,
    work_dir: str,
    sendable_exts: set[str] = SENDABLE_EXTS,
    size_limit: int = TG_FILE_SIZE_LIMIT,
) -> list[tuple[str, bool]]:
    """Extract file paths from response text that exist on disk and are worth sending."""
    seen = set()
    files = []

    for match in FILE_PATH_RE.finditer(text):
        path = match.group(1)
        if path.startswith("./"):
            path = os.path.join(work_dir, path[2:])
        path = os.path.abspath(path)

        if path in seen:
            continue
        seen.add(path)

        if not os.path.isfile(path):
            continue

        ext = os.path.splitext(path)[1].lower()
        if ext not in sendable_exts:
            continue

        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == 0 or size > size_limit:
            continue

        is_image = ext in IMAGE_EXTS
        files.append((path, is_image))

    return files


async def send_files_to_chat(
    chat,
    files: list[tuple[str, bool]],
    session_name: str = "",
    topic_thread_id: int | None = None,
    logger=None,
):
    """Send extracted files to Telegram chat as photos or documents."""
    from telegram import InputFile

    label = f"[{session_name}] " if session_name else ""

    for path, is_image in files:
        filename = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                if is_image:
                    await chat.send_photo(
                        photo=InputFile(f, filename=filename),
                        caption=f"{label}{filename}",
                        message_thread_id=topic_thread_id,
                    )
                else:
                    await chat.send_document(
                        document=InputFile(f, filename=filename),
                        caption=f"{label}{filename}",
                        message_thread_id=topic_thread_id,
                    )
            if logger:
                logger.info(f"Sent file to chat: {path}")
        except Exception as e:
            if logger:
                logger.warning(f"Failed to send file {path}: {e}")
