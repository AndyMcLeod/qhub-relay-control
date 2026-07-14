"""Mission log: a timestamped record of every command issued and every status
change, using the host computer's local clock -- the Q-Hub's wire protocol
carries no clock or timestamp of its own, so there is nothing to log against
but this machine's time.

One CSV file per local calendar day, in a `logs/` folder next to the
program, so a whole deployment's history survives power cycles and hands off
for review without special tooling -- it opens directly in a spreadsheet."""

import csv
import os
import threading
from datetime import datetime

LOG_DIR_NAME = "logs"
_COLUMNS = ["timestamp", "type", "channel_index", "channel_name", "event", "detail"]


def _local_timestamp():
    """Local wall-clock time, millisecond precision. Deliberately local, not
    UTC: this log is meant to be read by the person standing next to the
    machine, against the clock they can see."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Logbook:
    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, LOG_DIR_NAME)
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self._current_date = None
        self._file = None
        self._writer = None

    def _path_for(self, date_str):
        return os.path.join(self.dir, f"qhub-log-{date_str}.csv")

    def _ensure_file_locked(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._file is not None:
            return
        if self._file is not None:
            self._file.close()
        self._current_date = today
        path = self._path_for(today)
        is_new = not os.path.exists(path)
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if is_new:
            self._writer.writerow(_COLUMNS)
            self._file.flush()

    def _write(self, type_, index, name, event, detail):
        with self._lock:
            self._ensure_file_locked()
            self._writer.writerow([
                _local_timestamp(), type_,
                "" if index is None else index,
                name or "", event, detail or "",
            ])
            self._file.flush()

    def command(self, index, name, event, detail=""):
        self._write("command", index, name, event, detail)

    def status(self, event, detail=""):
        self._write("status", None, "", event, detail)

    def today_path(self):
        with self._lock:
            self._ensure_file_locked()
            return self._path_for(self._current_date)

    def list_dates(self):
        if not os.path.isdir(self.dir):
            return []
        dates = []
        prefix, suffix = "qhub-log-", ".csv"
        for fname in os.listdir(self.dir):
            if fname.startswith(prefix) and fname.endswith(suffix):
                dates.append(fname[len(prefix):-len(suffix)])
        return sorted(dates)

    def path_for_date(self, date_str):
        path = self._path_for(date_str)
        return path if os.path.exists(path) else None

    def tail(self, max_rows=200):
        """Most recent rows from today's log, for a quick in-browser look."""
        path = self.today_path()
        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        except OSError:
            return []
        if rows and rows[0] == _COLUMNS:
            rows = rows[1:]
        return rows[-max_rows:]

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
