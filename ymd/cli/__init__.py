"""
Интерфейс командной строки.

Важно: имя `main` внутри пакета — это подмодуль `ymd.cli.main`, а не функция,
поэтому точку входа импортируют так: `from ymd.cli.main import main`
или запускают приложение через `python -m ymd`.
"""

from ymd.cli.main import main as run
from ymd.cli.parser import create_parser, parse_args

__all__ = [
    "create_parser",
    "parse_args",
    "run",
]
