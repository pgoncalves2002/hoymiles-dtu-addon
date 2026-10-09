#!/usr/bin/env python3
"""Hoymiles DTU -> API REST (JSON).

Consulta periodicamente http://<dtu>/hometable.xml (a mesma fonte usada pela
pagina home.htm do DTU) e expoe a ultima leitura em JSON.

Sem dependencias externas: apenas biblioteca padrao do Python 3.
"""
import http.client
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPTIONS_FILE = "/data/options.json"  # gerado pelo Home Assistant Supervisor
HEADER_FIELDS = 12                   # campos antes da lista de paineis
ROW_FIELDS = 8                       # campos por painel


# ----------------------------------------------------------------- config --
def load_options():
    opts = {"dtu_host": "192.168.0.34", "poll_interval": 5, "timeout": 5}
    try:
        with open(OPTIONS_FILE, encoding="utf-8") as fh:
            opts.update(json.load(fh))
    except FileNotFoundError:
        pass
    # Variaveis de ambiente tem prioridade (uteis fora do Home Assistant)
    if os.environ.get("DTU_HOST"):
        opts["dtu_host"] = os.environ["DTU_HOST"]
    if os.environ.get("POLL_INTERVAL"):
        opts["poll_interval"] = int(os.environ["POLL_INTERVAL"])
    if os.environ.get("TIMEOUT"):
        opts["timeout"] = int(os.environ["TIMEOUT"])
    opts["port"] = int(os.environ.get("PORT", 8099))
    return opts


# ----------------------------------------------------------------- parser --
def _num(value):
    """'   0. 0V' -> 0.0 | '1865Wh' -> 1865.0 | '28.14' -> 28.14"""
    cleaned = re.sub(r"[^0-9.\-]", "", value or "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _int(value, default=0):
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _timestamp(value):
    """O DTU devolve '2000-00-00 00:00:00' quando nao ha leitura do painel."""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").isoformat()
    except (ValueError, AttributeError):
        return None


def parse_hometable(text):
    """Converte o conteudo de hometable.xml em um dicionario."""
    text = text.split("\x00", 1)[0]  # a resposta vem preenchida com NULs
    fields = [f.strip() for f in text.split(";")]
    if len(fields) < HEADER_FIELDS:
        raise ValueError("resposta do DTU com formato inesperado")

    declared = _int(fields[4])
    has_list = _int(fields[5]) == 1
    available = (len(fields) - HEADER_FIELDS) // ROW_FIELDS
    count = min(declared, available) if has_list else 0

    panels = []
    for i in range(count):
        row = fields[HEADER_FIELDS + i * ROW_FIELDS:HEADER_FIELDS + (i + 1) * ROW_FIELDS]
        panel_id = row[0]
        if not panel_id:
            continue
        serial, _, port = panel_id.partition("-")
        v_grid = _num(row[2])
        panels.append({
            "id": panel_id,
            "inverter_serial": serial,
            "port": _int(port, None),
            "v_pv": _num(row[1]),            # V
            "v_grid": v_grid,                # V
            "f_grid": _num(row[3]),          # Hz
            "p_grid": _num(row[4]),          # W
            "e_today_wh": _num(row[5]),      # Wh
            "temperature": _num(row[6]),     # graus C
            "last_update": _timestamp(row[7]),  # horario do DTU, sem fuso
            "online": bool(v_grid),          # derivado: tensao de rede > 0
            "raw": dict(zip(
                ("v_pv", "v_grid", "f_grid", "p_grid", "e_today", "temp", "time"),
                row[1:])),
        })

    inverters = {}
    for p in panels:
        inv = inverters.setdefault(p["inverter_serial"], {
            "serial": p["inverter_serial"], "panels": [], "p_grid": 0.0,
            "e_today_wh": 0.0, "temperature": None, "online": False,
        })
        inv["panels"].append(p["id"])
        inv["p_grid"] = round(inv["p_grid"] + (p["p_grid"] or 0), 2)
        inv["e_today_wh"] = round(inv["e_today_wh"] + (p["e_today_wh"] or 0), 2)
        inv["online"] = inv["online"] or p["online"]
        if p["temperature"] is not None:
            inv["temperature"] = max(inv["temperature"] or p["temperature"], p["temperature"])

    return {
        "summary": {
            "p_total_kw": _num(fields[0]),
            "e_total_kwh": _num(fields[1]),
            "e_today_kwh": _num(fields[2]),
            "co2_saved_kg": _num(fields[3]),
            "panel_count": declared,
            "panels_online": _int(fields[7]),
            "panels_offline": _int(fields[8]),
            "server_status": fields[9],
            "server_address": fields[10],
            "server_port": _int(fields[11], None),
        },
        "panels": panels,
        "inverters": list(inverters.values()),
    }


# ------------------------------------------------------------------ coleta --
class Collector:
    def __init__(self, host, interval, timeout):
        self.url = "http://%s/hometable.xml" % host
        self.host = host
        self.interval = max(1, int(interval))
        self.timeout = int(timeout)
        self._lock = threading.Lock()
        self._data = None
        self._fetched_at = None
        self._last_error = None
        self._last_attempt = None

    def _fetch(self):
        url = "%s?_t=%d" % (self.url, int(time.time() * 1000))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                raw = resp.read()
        except http.client.IncompleteRead as exc:  # servidor embarcado simples
            raw = exc.partial
        return raw.decode("latin-1")

    def poll_once(self):
        now = time.time()
        try:
            data = parse_hometable(self._fetch())
            with self._lock:
                self._data, self._fetched_at, self._last_error = data, now, None
        except Exception as exc:  # noqa: BLE001 - mantem a ultima leitura boa
            with self._lock:
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
            print("[coletor] falha ao ler %s: %s" % (self.url, exc), flush=True)
        finally:
            with self._lock:
                self._last_attempt = now

    def run_forever(self):
        while True:
            started = time.time()
            self.poll_once()
            time.sleep(max(0.5, self.interval - (time.time() - started)))

    def snapshot(self):
        with self._lock:
            data, fetched, error = self._data, self._fetched_at, self._last_error
        age = None if fetched is None else round(time.time() - fetched, 1)
        meta = {
            "dtu_host": self.host,
            "poll_interval_s": self.interval,
            "fetched_at": None if fetched is None else
            datetime.fromtimestamp(fetched, timezone.utc).isoformat(timespec="seconds"),
            "age_s": age,
            # leitura velha: o DTU nao respondeu nas ultimas 3 tentativas
            "stale": age is None or age > self.interval * 3 + self.timeout,
            "last_error": error,
        }
        return data, meta


# --------------------------------------------------------------------- API --
ENDPOINTS = {
    "/api/status": "leitura completa (resumo + paineis + microinversores)",
    "/api/summary": "totais do sistema",
    "/api/panels": "lista de paineis",
    "/api/panels/<id>": "um painel, ex.: /api/panels/100000000001-1",
    "/api/inverters": "paineis agrupados por microinversor",
    "/health": "estado do coletor",
}


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HoymilesDtuApi/1.0"

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            data, meta = collector.snapshot()

            if path == "/":
                return self._send(200, {"name": "Hoymiles DTU API", "endpoints": ENDPOINTS})
            if path == "/health":
                return self._send(200, {"ok": data is not None and not meta["stale"], **meta})
            if not path.startswith("/api/"):
                return self._send(404, {"error": "rota nao encontrada", "endpoints": ENDPOINTS})
            if data is None:
                return self._send(503, {"error": "ainda sem leitura do DTU", "meta": meta})

            if path == "/api/status":
                return self._send(200, {"meta": meta, **data})
            if path == "/api/summary":
                return self._send(200, {"meta": meta, "summary": data["summary"]})
            if path == "/api/panels":
                return self._send(200, {"meta": meta, "panels": data["panels"]})
            if path == "/api/inverters":
                return self._send(200, {"meta": meta, "inverters": data["inverters"]})
            if path.startswith("/api/panels/"):
                wanted = path[len("/api/panels/"):]
                for panel in data["panels"]:
                    if panel["id"] == wanted:
                        return self._send(200, {"meta": meta, "panel": panel})
                return self._send(404, {"error": "painel nao encontrado: " + wanted})
            return self._send(404, {"error": "rota nao encontrada", "endpoints": ENDPOINTS})

        def log_message(self, fmt, *args):  # silencia o log por requisicao
            pass

    return Handler


def main():
    opts = load_options()
    collector = Collector(opts["dtu_host"], opts["poll_interval"], opts["timeout"])
    threading.Thread(target=collector.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", opts["port"]), make_handler(collector))
    print("[api] DTU em %s, coleta a cada %ss, ouvindo na porta %s"
          % (opts["dtu_host"], collector.interval, opts["port"]), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
