"""Explicit downloader contract used by cleanup; no global MoviePilot state."""

from typing import Any, Callable


class DownloaderUnavailable(RuntimeError):
    pass


class DownloaderAdapter:
    def __init__(self, downloader: Any, *, normalize: Callable, identify: Callable):
        self.downloader = downloader
        self.normalize = normalize
        self.identify = identify

    def snapshot(self) -> dict[str, dict]:
        try:
            rows, error = self.downloader.get_torrents()
            if error or rows is None:
                raise DownloaderUnavailable("无法读取下载器状态")
            return {str(self.identify(row)): {**self.normalize(row), "hash": str(self.identify(row))}
                    for row in rows if self.identify(row)}
        except Exception as exc:
            raise DownloaderUnavailable("无法读取下载器状态，本次不提交删除") from exc

    def reannounce(self, torrent_hashes: list[str]) -> bool:
        """强制重新汇报 Tracker（站点 H&R 倒计时按 announce 结算，删除前应触发）。

        返回 False 表示下载器不支持（如 Transmission）或调用失败；调用方按
        基线闸门的保守判定继续，不因 reannounce 失败阻断删除。
        """
        client = getattr(self.downloader, "qbc", None)
        method = getattr(client, "torrents_reannounce", None)
        if not method or not torrent_hashes:
            return False
        try:
            method(torrent_hashes=torrent_hashes)
            return True
        except Exception:
            return False

    def remove(self, torrent_hash: str, *, delete_data: bool) -> str:
        try:
            result = self.downloader.delete_torrents(ids=[torrent_hash], delete_file=delete_data)
        except Exception:
            return "uncertain"  # The server may have received the request.
        # MoviePilot's qB wrapper catches transport exceptions and returns False
        # (app/modules/qbittorrent/qbittorrent.py::delete_torrents). False therefore
        # does NOT prove rejection; the server may have accepted a lost response.
        return "accepted" if result is True else "uncertain"
