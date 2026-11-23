"""HH-Auto-Response — автоматические отклики на вакансии hh.ru с сопроводительным письмом."""

# Версия пакета используется в журнале и User-Agent API-клиента.
__version__ = "1.1.4-beta"

# HTTPS-запросы (нейросеть, Telegram) — с системными сертификатами, если у Python своих нет.
from .core.certs import ensure_ca_bundle as _ensure_ca_bundle  # noqa: E402

_ensure_ca_bundle()
