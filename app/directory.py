"""Employee directory: maps a WhatsApp number to the employee's eIA number.

Loaded once at startup from a CSV with the columns phone, eia_no, name.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from app.validation import normalise_phone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EIA_NO_RE = re.compile(r"^[0-9]{13}$")


@dataclass(frozen=True)
class Employee:
    phone: str  # +91XXXXXXXXXX
    eia_no: str
    name: str


class EmployeeDirectory:
    def __init__(self, employees: list[Employee]):
        self._by_phone = {e.phone: e for e in employees}

    @classmethod
    def load(cls, path: str) -> "EmployeeDirectory":
        """Read the CSV. A relative path is resolved against the project root."""
        file = Path(path)
        if not file.is_absolute():
            file = PROJECT_ROOT / file
        employees = []
        with file.open(newline="", encoding="utf-8") as f:
            for line_no, row in enumerate(csv.DictReader(f), start=2):
                phone = normalise_phone(row.get("phone"))
                eia_no = (row.get("eia_no") or "").strip()
                if phone is None or not EIA_NO_RE.match(eia_no):
                    raise ValueError(f"{file}: line {line_no} needs a valid phone and a 13-digit eia_no")
                employees.append(Employee(phone=phone, eia_no=eia_no, name=(row.get("name") or "").strip()))
        return cls(employees)

    def lookup(self, phone: str | None) -> Employee | None:
        return self._by_phone.get(phone) if phone else None

    def __len__(self) -> int:
        return len(self._by_phone)
