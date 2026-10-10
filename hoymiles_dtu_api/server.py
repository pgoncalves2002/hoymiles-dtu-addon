#!/usr/bin/env python3
"""Hoymiles DTU -> API REST (JSON).

Consulta periodicamente http://<dtu>/hometable.xml (a mesma fonte usada pela
pagina home.htm do DTU) e expoe a ultima leitura em JSON.

O IP do DTU pode ser trocado em tempo real (painel do add-on no Home
Assistant ou POST /api/config); a troca e salva nas opcoes do add-on via
Supervisor, entao vale tambem depois de reiniciar.

Sem dependencias externas: apenas biblioteca padrao do Python 3.
"""
import concurrent.futures
import errno
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPTIONS_FILE = "/data/options.json"  # gerado pelo Home Assistant Supervisor
SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"
SUPERVISOR_NETWORK_URL = "http://supervisor/network/info"
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
# redes internas do Supervisor/Docker: nunca entram na busca
INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("172.30.32.0/23", "172.30.232.0/23")]
AUTO_SCAN_EVERY = 600  # s entre buscas automaticas enquanto o DTU nao responde
HEADER_FIELDS = 12                   # campos antes da lista de paineis
ROW_FIELDS = 8                       # campos por painel
VERSION = "1.3.0"
# IP ou nome na rede, com porta opcional: 192.168.0.34 | dtu.lan | 192.168.0.34:8080
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")


# ----------------------------------------------------------------- config --
def load_options():
    opts = {"dtu_host": "192.168.0.34", "poll_interval": 5, "timeout": 5,
            "auto_discover": True, "mqtt_enabled": True}
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
    for key in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD"):
        if os.environ.get(key):
            opts[key.lower()] = os.environ[key]
    if os.environ.get("MQTT_ENABLED"):
        opts["mqtt_enabled"] = os.environ["MQTT_ENABLED"].lower() in ("1", "true", "sim", "yes")
    if os.environ.get("AUTO_DISCOVER"):
        opts["auto_discover"] = os.environ["AUTO_DISCOVER"].lower() in ("1", "true", "sim", "yes")
    opts["port"] = int(os.environ.get("PORT", 8099))
    return opts


def normalize_host(value):
    """Aceita '192.168.0.40', 'http://192.168.0.40/' etc. Devolve None se invalido."""
    host = (value or "").strip()
    host = re.sub(r"^https?://", "", host, flags=re.I).split("/", 1)[0]
    if not HOST_RE.match(host):
        return None
    _, _, port = host.partition(":")
    if port and not 0 < int(port) < 65536:
        return None
    return host


_port_cache = {"at": 0.0, "value": None}


def external_api_port(internal=8099):
    """Porta externa da API (secao Rede do add-on), via Supervisor; cache de 60 s.

    Devolve None quando a porta esta desativada no add-on.
    """
    if time.time() - _port_cache["at"] < 60:
        return _port_cache["value"]
    value = internal
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        req = urllib.request.Request(SUPERVISOR_INFO_URL,
                                     headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                network = json.loads(resp.read()).get("data", {}).get("network") or {}
            value = network.get("%d/tcp" % internal, internal)
        except Exception as exc:  # noqa: BLE001
            print("[api] nao consegui ler a porta no Supervisor: %s" % exc, flush=True)
    _port_cache.update(at=time.time(), value=value)
    return value


def persist_options(options):
    """Grava as opcoes do add-on pelo Supervisor (aparecem na aba Configuracao).

    Devolve (ok, erro). Fora do Home Assistant nao ha Supervisor: a troca vale
    apenas ate o processo reiniciar.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False, "sem Supervisor (rodando fora do Home Assistant)"
    req = urllib.request.Request(
        SUPERVISOR_OPTIONS_URL,
        data=json.dumps({"options": options}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read() or b"{}")
        if result.get("result") == "ok":
            return True, None
        return False, result.get("message") or "resposta inesperada do Supervisor"
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read()).get("message")
        except Exception:  # noqa: BLE001
            msg = None
        return False, "Supervisor HTTP %s%s" % (exc.code, ": " + msg if msg else "")
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


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
def _fetch_raw(host, timeout):
    """GET minimo por socket, para servidores embarcados que o http.client recusa
    (sem linha de status, cabecalhos malformados, conexao fechada cedo)."""
    name, _, port = host.partition(":")
    request = ("GET /hometable.xml HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n"
               % host).encode("ascii")
    chunks = []
    with socket.create_connection((name, int(port or 80)), timeout=timeout) as sock:
        sock.sendall(request)
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                if chunks:  # alguns DTUs nao fecham a conexao: fica com o que chegou
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    if raw.startswith(b"HTTP/"):
        head, _, raw = raw.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].split(b" ")
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        if code != 200:
            raise urllib.error.HTTPError("http://%s/hometable.xml" % host, code,
                                         "HTTP %d" % code, None, None)
    return raw


def fetch_hometable(host, timeout):
    url = "http://%s/hometable.xml?_t=%d" % (host, int(time.time() * 1000))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except http.client.IncompleteRead as exc:  # servidor embarcado simples
        raw = exc.partial
    except http.client.HTTPException:  # BadStatusLine, LineTooLong...
        raw = _fetch_raw(host, timeout)
    except urllib.error.URLError as exc:
        if isinstance(exc, urllib.error.HTTPError) or not isinstance(
                exc.reason, (http.client.RemoteDisconnected, ConnectionResetError)):
            raise
        raw = _fetch_raw(host, timeout)
    return raw.decode("latin-1")


def explain_error(exc, timeout):
    """Traduz a excecao da leitura em uma dica do que provavelmente aconteceu."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return ("o aparelho nesse IP respondeu, mas não tem /hometable.xml: "
                    "provavelmente não é o DTU")
        return "o aparelho nesse IP respondeu com erro HTTP %d" % exc.code
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "nome não encontrado na rede (DNS)"
    if isinstance(reason, TimeoutError):  # inclui socket.timeout
        return ("ninguém respondeu em %ds: IP errado, DTU fora do Wi-Fi ou desligado"
                % timeout)
    if isinstance(reason, ConnectionRefusedError):
        return "o IP existe, mas recusou a conexão na porta 80: não parece ser o DTU"
    if isinstance(reason, OSError) and reason.errno in (
            errno.EHOSTUNREACH, errno.EHOSTDOWN, errno.ENETUNREACH):
        return ("nenhum aparelho respondeu nesse IP: IP errado (mudou?) ou DTU fora "
                "da rede")
    if isinstance(exc, ValueError):
        return "o aparelho respondeu, mas a resposta não tem o formato do DTU"
    return None


def parse_dtu(text):
    """parse_hometable com verificacao: recusa resposta de outro aparelho
    (pagina de roteador etc.) que por acaso tenha ';' suficientes."""
    head = text.split("\x00", 1)[0]
    if "<" in head[:200]:  # HTML/XML de verdade nao e o formato do DTU
        raise ValueError("a resposta é uma página HTML, não o formato do DTU")
    data = parse_hometable(head)
    s = data["summary"]
    if None in (s["p_total_kw"], s["e_total_kwh"], s["e_today_kwh"]):
        raise ValueError("a resposta não tem o formato do DTU")
    return data


# ------------------------------------------------------------------- busca --
def _scan_subnets(current_host):
    """Redes /24 onde procurar: as do Home Assistant (via Supervisor) e a do IP atual."""
    nets = []
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        req = urllib.request.Request(SUPERVISOR_NETWORK_URL,
                                     headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                interfaces = json.loads(resp.read()).get("data", {}).get("interfaces") or []
            for iface in interfaces:
                if iface.get("enabled") is False or iface.get("connected") is False:
                    continue
                for addr in (iface.get("ipv4") or {}).get("address") or []:
                    itf = ipaddress.ip_interface(addr)
                    nets.append(ipaddress.ip_network("%s/%d" % (
                        itf.ip, max(24, itf.network.prefixlen)), strict=False))
        except Exception as exc:  # noqa: BLE001
            print("[busca] nao consegui ler as redes no Supervisor: %s" % exc, flush=True)
    try:
        ip = ipaddress.ip_address(current_host.split(":", 1)[0])
        nets.append(ipaddress.ip_network("%s/24" % ip, strict=False))
    except ValueError:
        pass  # dtu_host e um nome, nao um IP
    seen, result = set(), []
    for net in nets:
        if (net.version == 4 and net.is_private and net not in seen
                and not any(net.overlaps(n) for n in INTERNAL_NETS)):
            seen.add(net)
            result.append(net)
    return result[:4]


def _probe(ip, timeout):
    try:  # filtro rapido: so faz a leitura onde a porta 80 esta aberta
        with socket.create_connection((ip, 80), timeout=1.5):
            pass
    except OSError:
        return None
    try:
        data = parse_dtu(fetch_hometable(ip, timeout))
    except Exception:  # noqa: BLE001
        return None
    s = data["summary"]
    return {"host": ip, "panel_count": s["panel_count"], "panels_online": s["panels_online"],
            "p_total_kw": s["p_total_kw"], "e_total_kwh": s["e_total_kwh"]}


_scan_lock = threading.Lock()


def scan_for_dtus(current_host, timeout):
    """Procura DTUs nas redes do Home Assistant. Leva ~5-15 s."""
    with _scan_lock:
        started = time.time()
        nets = _scan_subnets(current_host)
        hosts = [str(h) for net in nets for h in net.hosts()]
        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
            found = [r for r in pool.map(lambda h: _probe(h, timeout), hosts) if r]
        result = {
            "subnets": [str(n) for n in nets],
            "scanned": len(hosts),
            "elapsed_s": round(time.time() - started, 1),
            "found": found,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        print("[busca] %d IPs em %s: %s" % (len(hosts), ", ".join(result["subnets"]) or "-",
              ", ".join(f["host"] for f in found) or "nenhum DTU"), flush=True)
        return result


# ------------------------------------------------------------------ coletor --
class Collector:
    def __init__(self, host, interval, timeout, auto_discover=True, extra_options=None):
        self.host = host
        # demais opcoes do add-on (mqtt_*): reenviadas ao salvar, senao o Supervisor as apaga
        self.extra_options = dict(extra_options or {})
        self.mqtt = None  # MqttBridge, quando ligado
        self.interval = max(1, int(interval))
        self.timeout = int(timeout)
        self.auto_discover = bool(auto_discover)
        self._lock = threading.Lock()
        self._data = None
        self._fetched_at = None
        self._last_error = None
        self._last_hint = None
        self._failures = 0
        self._last_attempt = None
        self._fetch_ms = None
        self._last_scan_at = 0.0
        self._auto_scan = None  # resultado da ultima busca automatica

    @property
    def url(self):
        return "http://%s/hometable.xml" % self.host

    def options(self):
        """Opcoes no formato do schema do add-on (config.yaml)."""
        return {**self.extra_options,
                "dtu_host": self.host, "poll_interval": self.interval,
                "timeout": self.timeout, "auto_discover": self.auto_discover}

    def set_host(self, host, data=None):
        """Troca o DTU consultado sem reiniciar.

        `data` e a leitura de teste feita no novo endereco (se houver); sem
        ela, a leitura antiga e descartada para nao misturar os dois DTUs.
        """
        with self._lock:
            old, self.host = self.host, host
            self._data = data
            self._fetched_at = time.time() if data is not None else None
            self._last_error = self._last_hint = None
            self._failures = 0
        print("[coletor] DTU trocado: %s -> %s" % (old, host), flush=True)

    def poll_once(self):
        now = time.time()
        with self._lock:
            host = self.host
        try:
            data = parse_dtu(fetch_hometable(host, self.timeout))
            with self._lock:
                if host != self.host:  # o IP mudou durante a leitura
                    return
                self._data, self._fetched_at = data, now
                self._last_error = self._last_hint = None
                self._failures = 0
                self._fetch_ms = round((time.time() - now) * 1000)
        except Exception as exc:  # noqa: BLE001 - mantem a ultima leitura boa
            hint = explain_error(exc, self.timeout)
            with self._lock:
                if host != self.host:
                    return
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
                self._last_hint = hint
                self._failures += 1
            print("[coletor] falha ao ler %s: %s%s" % (host, exc, " (%s)" % hint if hint else ""),
                  flush=True)
        finally:
            with self._lock:
                self._last_attempt = now

    def _maybe_auto_discover(self):
        """Se o DTU sumiu (3 falhas seguidas), procura na rede e troca de IP sozinho.

        Faz no maximo uma busca a cada AUTO_SCAN_EVERY segundos; so troca quando
        acha exatamente um DTU, para nao escolher errado com dois na rede.
        """
        with self._lock:
            due = (self.auto_discover and self._failures >= 3
                   and time.time() - self._last_scan_at >= AUTO_SCAN_EVERY)
            host = self.host
        if not due:
            return
        self._last_scan_at = time.time()
        print("[busca] DTU sem responder em %s; procurando na rede..." % host, flush=True)
        result = scan_for_dtus(host, self.timeout)
        others = [f for f in result["found"] if f["host"] != host]
        result["switched_to"] = None
        if len(result["found"]) == 1 and others:
            new = others[0]["host"]
            self.set_host(new)
            ok, err = persist_options(self.options())
            result["switched_to"] = new
            print("[busca] trocado automaticamente para %s%s" % (
                new, "" if ok else " (nao salvo nas opcoes: %s)" % err), flush=True)
            self.poll_once()
        elif len(result["found"]) > 1:
            print("[busca] mais de um DTU encontrado; escolha no painel do add-on", flush=True)
        self._auto_scan = result

    def run_forever(self):
        while True:
            started = time.time()
            self.poll_once()
            try:
                self._maybe_auto_discover()
            except Exception as exc:  # noqa: BLE001
                print("[busca] falhou: %s" % exc, flush=True)
            time.sleep(max(0.5, self.interval - (time.time() - started)))

    def snapshot(self):
        with self._lock:
            data, fetched, error = self._data, self._fetched_at, self._last_error
            host, fetch_ms, hint = self.host, self._fetch_ms, self._last_hint
            failures, auto_scan = self._failures, self._auto_scan
        age = None if fetched is None else round(time.time() - fetched, 1)
        meta = {
            "dtu_host": host,
            "poll_interval_s": self.interval,
            "fetched_at": None if fetched is None else
            datetime.fromtimestamp(fetched, timezone.utc).isoformat(timespec="seconds"),
            "age_s": age,
            "fetch_ms": fetch_ms,  # tempo de resposta do DTU na ultima leitura boa
            # leitura velha: o DTU nao respondeu nas ultimas 3 tentativas
            "stale": age is None or age > self.interval * 3 + self.timeout,
            "last_error": error,
            "last_error_hint": hint,
            "consecutive_failures": failures,
            "auto_discover": self.auto_discover,
            "last_auto_scan": auto_scan,
            "mqtt": self.mqtt.status() if self.mqtt else None,
        }
        return data, meta


# -------------------------------------------------------------------- mqtt --
# Publica as leituras no broker MQTT com MQTT Discovery do Home Assistant: os
# sensores aparecem sozinhos (dispositivo "DTU Hoymiles" + um por
# microinversor) e "Energia total" pode ser usada no painel Energia.
MQTT_BASE = "hoymiles_dtu"
MQTT_STATE = MQTT_BASE + "/state"
MQTT_AVAIL = MQTT_BASE + "/availability"
MQTT_DISCOVERY = "homeassistant"
MQTT_STATE_FILE = "/data/mqtt_state.json"  # maior energia total ja publicada

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:  # fora do container do add-on, sem paho-mqtt instalado
    paho_mqtt = None


def _r(value, digits):
    return None if value is None else round(value, digits)


class MqttBridge:
    def __init__(self, collector, options):
        self.collector = collector
        self.enabled = bool(options.get("mqtt_enabled", True))
        self.manual = {k: options.get(k) for k in
                       ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password")}
        self.state_file = MQTT_STATE_FILE
        self._client = None
        self._lock = threading.Lock()
        self._connected = False
        self._broker = None
        self._error = None
        self._published = {}       # topico -> payload ja publicado (evita repetir)
        self._entities = 0
        self._last_state_at = None
        self._energy_max = self._load_energy_max()
        self._drop_logged = None

    # ----------------------------------------------------------- estado --
    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "connected": self._connected,
                "broker": self._broker,
                "error": self._error,
                "entities": self._entities,
                "last_publish": self._last_state_at,
            }

    def _set(self, **kw):
        with self._lock:
            for k, v in kw.items():
                setattr(self, "_" + k, v)

    def _load_energy_max(self):
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                return float(json.load(fh).get("energy_total_kwh") or 0)
        except (OSError, ValueError):
            return 0.0

    def _save_energy_max(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump({"energy_total_kwh": self._energy_max}, fh)
        except OSError:
            pass  # fora do HA nao ha /data: so perde a protecao apos reiniciar

    # ---------------------------------------------------------- conexao --
    def _broker_settings(self):
        """Broker manual (opcoes mqtt_*) ou o Mosquitto do HA, via Supervisor."""
        if self.manual.get("mqtt_host"):
            return {"host": self.manual["mqtt_host"],
                    "port": int(self.manual.get("mqtt_port") or 1883),
                    "username": self.manual.get("mqtt_user"),
                    "password": self.manual.get("mqtt_password"), "ssl": False}
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("sem Supervisor: preencha mqtt_host para usar MQTT")
        req = urllib.request.Request(SUPERVISOR_MQTT_URL,
                                     headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read()).get("data") or {}
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                raise RuntimeError("nenhum broker MQTT no Home Assistant: instale o "
                                   "add-on Mosquitto broker (ou preencha mqtt_host)")
            raise
        if not data.get("host"):
            raise RuntimeError("o Supervisor nao informou o broker MQTT")
        return data

    def start(self):
        if not self.enabled:
            print("[mqtt] desligado nas opcoes", flush=True)
            return
        if paho_mqtt is None:
            self._set(error="biblioteca paho-mqtt ausente")
            print("[mqtt] paho-mqtt nao instalado; MQTT desligado", flush=True)
            return
        threading.Thread(target=self._run, daemon=True, name="mqtt").start()

    def _run(self):
        # descobre o broker (tenta de novo ate o Mosquitto subir)
        while True:
            try:
                cfg = self._broker_settings()
                break
            except Exception as exc:  # noqa: BLE001
                self._set(error=str(exc))
                print("[mqtt] %s; tentando de novo em 60 s" % exc, flush=True)
                time.sleep(60)
        self._set(broker="%s:%s" % (cfg["host"], cfg["port"]))
        client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="hoymiles-dtu-api")
        if cfg.get("username"):
            client.username_pw_set(cfg["username"], cfg.get("password"))
        if cfg.get("ssl"):
            client.tls_set()
        client.will_set(MQTT_AVAIL, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=60)
        self._client = client
        client.connect_async(cfg["host"], int(cfg["port"]), keepalive=60)
        client.loop_start()
        print("[mqtt] conectando em %s:%s" % (cfg["host"], cfg["port"]), flush=True)
        while True:
            try:
                self.publish()
            except Exception as exc:  # noqa: BLE001
                print("[mqtt] falha ao publicar: %s" % exc, flush=True)
            time.sleep(self.collector.interval)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self._set(connected=False, error="broker recusou: %s" % reason_code)
            print("[mqtt] broker recusou a conexao: %s" % reason_code, flush=True)
            return
        self._set(connected=True, error=None)
        print("[mqtt] conectado", flush=True)
        client.subscribe(MQTT_DISCOVERY + "/status")
        with self._lock:
            self._published.clear()  # republica tudo apos (re)conectar
        threading.Thread(target=self.publish, daemon=True).start()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._set(connected=False)
        if reason_code.is_failure:
            self._set(error="desconectado: %s" % reason_code)
            print("[mqtt] desconectado: %s" % reason_code, flush=True)

    def _on_message(self, client, userdata, msg):
        # o HA reiniciou: manda de novo discovery e estado
        if msg.topic == MQTT_DISCOVERY + "/status" and msg.payload == b"online":
            with self._lock:
                self._published.clear()
            threading.Thread(target=self.publish, daemon=True).start()

    def _send(self, topic, payload, retain=True):
        with self._lock:
            if self._published.get(topic) == payload:
                return
        info = self._client.publish(topic, payload, qos=1, retain=retain)
        if info.rc == paho_mqtt.MQTT_ERR_SUCCESS:
            with self._lock:
                self._published[topic] = payload

    # ------------------------------------------------------- publicacao --
    def build_state(self, data):
        s = data["summary"]
        panel_sum = sum(p["p_grid"] or 0 for p in data["panels"])
        power_w = round(panel_sum, 1) if data["panels"] else _r(
            None if s["p_total_kw"] is None else s["p_total_kw"] * 1000, 1)
        total = s["e_total_kwh"]
        if total is not None and total > 0:
            if total < self._energy_max:
                # o painel Energia conta qualquer queda como "zerou" e somaria tudo de
                # novo na volta: nunca publica um total menor que o ja publicado
                if total != self._drop_logged:
                    self._drop_logged = total
                    print("[mqtt] energia total do DTU caiu (%.2f < %.2f); mantendo a maior"
                          % (total, self._energy_max), flush=True)
                total = self._energy_max
            elif total > self._energy_max:
                self._energy_max = total
                self._save_energy_max()
        else:
            total = self._energy_max or None  # 0/vazio do DTU nao entra
        return {
            "power_w": power_w,
            "energy_total_kwh": total,
            "energy_today_kwh": s["e_today_kwh"],
            "co2_saved_kg": s["co2_saved_kg"],
            "panels_online": s["panels_online"],
            "panel_count": s["panel_count"],
            "inv": {i["serial"]: {
                "power_w": i["p_grid"],
                "energy_today_wh": i["e_today_wh"],
                "temperature": i["temperature"] if i["online"] else None,
            } for i in data["inverters"]},
            "pnl": {p["id"]: {
                "power_w": p["p_grid"],
                "energy_today_wh": p["e_today_wh"],
            } for p in data["panels"]},
        }

    def discovery(self, data):
        """Mensagens de configuracao do MQTT Discovery: [(topico, payload)]."""
        dtu_dev = {"identifiers": [MQTT_BASE], "name": "DTU Hoymiles",
                   "manufacturer": "Hoymiles", "model": "DTU",
                   "sw_version": "add-on " + VERSION}
        origin = {"name": "Hoymiles DTU API", "sw_version": VERSION}
        out = []

        def sensor(uid, name, template, device, unit=None, device_class=None,
                   state_class=None, precision=None, icon=None, category=None):
            cfg = {"name": name, "unique_id": uid, "default_entity_id": "sensor." + uid,
                   "state_topic": MQTT_STATE, "value_template": template,
                   "availability_topic": MQTT_AVAIL, "device": device, "origin": origin}
            for key, val in (("unit_of_measurement", unit), ("device_class", device_class),
                             ("state_class", state_class), ("icon", icon),
                             ("suggested_display_precision", precision),
                             ("entity_category", category)):
                if val is not None:
                    cfg[key] = val
            out.append(("%s/sensor/%s/%s/config" % (MQTT_DISCOVERY, MQTT_BASE, uid),
                        json.dumps(cfg, ensure_ascii=False, separators=(",", ":"))))

        sensor("hoymiles_dtu_power", "Potência", "{{ value_json.power_w }}", dtu_dev,
               "W", "power", "measurement", 0)
        sensor("hoymiles_dtu_energy_total", "Energia total",
               "{{ value_json.energy_total_kwh }}", dtu_dev,
               "kWh", "energy", "total_increasing", 2)
        sensor("hoymiles_dtu_energy_today", "Energia hoje",
               "{{ value_json.energy_today_kwh }}", dtu_dev,
               "kWh", "energy", "total_increasing", 2)
        sensor("hoymiles_dtu_co2_saved", "CO₂ evitado", "{{ value_json.co2_saved_kg }}",
               dtu_dev, "kg", None, "total_increasing", 1, "mdi:molecule-co2")
        sensor("hoymiles_dtu_panels_online", "Painéis online",
               "{{ value_json.panels_online }}", dtu_dev, None, None, "measurement",
               0, "mdi:solar-panel", "diagnostic")

        for inv in data["inverters"]:
            serial = inv["serial"]
            dev = {"identifiers": ["%s_%s" % (MQTT_BASE, serial)],
                   "name": "Microinversor %s" % serial, "manufacturer": "Hoymiles",
                   "model": "Microinversor", "serial_number": serial,
                   "via_device": MQTT_BASE}
            base = "{{ value_json.inv['%s']" % serial
            uid = "hoymiles_%s" % serial
            sensor(uid + "_power", "Potência", base + ".power_w }}", dev,
                   "W", "power", "measurement", 0)
            sensor(uid + "_energy_today", "Energia hoje", base + ".energy_today_wh }}", dev,
                   "Wh", "energy", "total_increasing", 0)
            sensor(uid + "_temperature", "Temperatura", base + ".temperature }}", dev,
                   "°C", "temperature", "measurement", 1)
            for pid in inv["panels"]:
                port = pid.partition("-")[2] or pid
                pbase = "{{ value_json.pnl['%s']" % pid
                puid = "hoymiles_%s" % pid.replace("-", "_")
                sensor(puid + "_power", "Painel %s potência" % port,
                       pbase + ".power_w }}", dev, "W", "power", "measurement", 0)
                sensor(puid + "_energy_today", "Painel %s energia hoje" % port,
                       pbase + ".energy_today_wh }}", dev,
                       "Wh", "energy", "total_increasing", 0)
        return out

    def publish(self):
        if self._client is None or not self.status()["connected"]:
            return
        data, meta = self.collector.snapshot()
        if data is None:
            self._send(MQTT_AVAIL, "offline")
            return
        configs = self.discovery(data)
        for topic, payload in configs:
            self._send(topic, payload)
        self._set(entities=len(configs))
        state = json.dumps(self.build_state(data), ensure_ascii=False,
                           separators=(",", ":"))
        before = self._published.get(MQTT_STATE)
        self._send(MQTT_STATE, state)
        if before != state:
            self._set(last_state_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        # DTU sem responder: sensores ficam "indisponivel" em vez de valor velho
        self._send(MQTT_AVAIL, "offline" if meta["stale"] else "online")


# --------------------------------------------------------------------- API --
ENDPOINTS = {
    "/api/status": "leitura completa (resumo + paineis + microinversores)",
    "/api/summary": "totais do sistema",
    "/api/panels": "lista de paineis",
    "/api/panels/<id>": "um painel, ex.: /api/panels/100000000001-1",
    "/api/inverters": "paineis agrupados por microinversor",
    "/api/config": "GET: configuracao atual | POST {\"dtu_host\": \"...\"}: troca o IP do DTU",
    "/api/refresh": "POST: le o DTU agora e devolve a leitura (para testes)",
    "/api/discover": "POST: procura DTUs na rede do Home Assistant (~5-15 s)",
    "/health": "estado do coletor",
}

MAX_BODY = 4096

PAGE_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DTU Hoymiles</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --fg:#1c1f24; --muted:#667085; --line:#d9dde3;
          --soft:#eef0f3; --accent:#0b7a75; --on-accent:#fff; --ok:#16794a; --bad:#b42318;
          --warn:#a15c07; --code-bg:#f2f4f7; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#111418; --card:#1b1f25; --fg:#e6e8eb; --muted:#98a2b3; --line:#2e343d;
            --soft:#232830; --accent:#3fb8b0; --on-accent:#0b1f1e; --ok:#4ac58a; --bad:#f97066;
            --warn:#f5b041; --code-bg:#14171c; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:960px; margin:0 auto; padding:20px 16px 32px; }
  header { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  h1 { font-size:20px; margin:0; }
  .ver { color:var(--muted); font-size:13px; }
  header .spacer { flex:1; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px; margin-bottom:16px; }
  nav { display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:16px;
        overflow-x:auto; }
  nav button { background:none; color:var(--muted); border:0; border-bottom:2px solid transparent;
               border-radius:0; padding:8px 12px; font-weight:600; white-space:nowrap; }
  nav button[aria-selected=true] { color:var(--fg); border-bottom-color:var(--accent); }
  [hidden] { display:none !important; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px;
           margin-bottom:16px; }
  .tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; }
  .tile .k { color:var(--muted); font-size:13px; }
  .tile .v { font-size:24px; font-weight:650; font-variant-numeric:tabular-nums; margin-top:2px; }
  .tile .v small { font-size:14px; font-weight:500; color:var(--muted); margin-left:3px; }
  dl { display:grid; grid-template-columns:auto 1fr; gap:6px 16px; margin:0; }
  dt { color:var(--muted); }
  dd { margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  button { padding:8px 14px; font:inherit; font-weight:600; color:var(--on-accent);
           background:var(--accent); border:0; border-radius:8px; cursor:pointer; }
  button.ghost { color:var(--fg); background:var(--soft); }
  button:disabled { opacity:.6; cursor:wait; }
  label { display:block; font-weight:600; margin-bottom:6px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  input[type=text], select { flex:1; min-width:0; padding:8px 11px; font:inherit; color:var(--fg);
         background:var(--bg); border:1px solid var(--line); border-radius:8px; }
  .check { display:flex; gap:8px; align-items:center; margin-top:10px; color:var(--muted);
           font-weight:400; }
  .msg { margin-top:12px; min-height:1.4em; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  .alert { border:1px solid var(--bad); background:color-mix(in srgb, var(--bad) 8%, var(--card));
           border-radius:12px; padding:14px 16px; margin-bottom:16px; }
  .alert b { color:var(--bad); }
  .alert p { margin:4px 0 10px; }
  .found { display:flex; gap:12px; align-items:center; justify-content:space-between;
           flex-wrap:wrap; padding:10px 0; border-top:1px solid var(--line); }
  .found:first-child { border-top:0; }
  .found code { font-size:15px; font-weight:600; }
  .muted { color:var(--muted); } .small { font-size:13px; }
  .table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:12px;
                background:var(--card); }
  table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
  th, td { padding:7px 10px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  th { font-size:13px; color:var(--muted); font-weight:600; background:var(--soft); }
  tr.inv td { background:var(--soft); font-weight:600; font-size:13px; }
  tr:last-child td { border-bottom:0; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px;
         background:var(--muted); vertical-align:middle; }
  .dot.on { background:var(--ok); }
  code, pre { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:13px; }
  .url { display:flex; gap:8px; align-items:center; background:var(--code-bg);
         border:1px solid var(--line); border-radius:8px; padding:6px 6px 6px 10px; margin-top:12px; }
  .url code { flex:1; overflow-x:auto; white-space:nowrap; }
  .url button { padding:4px 10px; font-size:13px; }
  .resp-meta { display:flex; gap:14px; flex-wrap:wrap; margin:12px 0 8px; font-size:13px; }
  pre { margin:0; background:var(--code-bg); border:1px solid var(--line); border-radius:8px;
        padding:12px; max-height:480px; overflow:auto; }
  .j-k { color:#7a3e9d; } .j-s { color:#16794a; } .j-n { color:#b54708; } .j-b { color:#175cd3; }
  @media (prefers-color-scheme: dark) {
    .j-k { color:#d6a6f5; } .j-s { color:#7fd4a3; } .j-n { color:#f5b866; } .j-b { color:#84adff; }
  }
</style>
</head>
<body>
<main>
  <header>
    <h1>DTU Hoymiles</h1><span class="ver" id="ver"></span>
    <span class="spacer"></span>
    <button class="ghost" id="readnow" title="Faz uma leitura do DTU agora">Ler agora</button>
  </header>

  <nav role="tablist">
    <button role="tab" data-tab="coleta">Coleta</button>
    <button role="tab" data-tab="paineis">Painéis</button>
    <button role="tab" data-tab="api">Testar API</button>
    <button role="tab" data-tab="ip">IP do DTU</button>
  </nav>

  <!-- Coleta -->
  <section data-panel="coleta">
    <div class="alert" id="alert" hidden>
      <b>O add-on não está conseguindo ler o DTU em <span id="a-host"></span></b>
      <p id="a-hint"></p>
      <button id="a-scan">Procurar DTU na rede</button>
    </div>
    <div class="tiles">
      <div class="tile"><div class="k">Potência agora</div><div class="v" id="t-power">—</div></div>
      <div class="tile"><div class="k">Energia hoje</div><div class="v" id="t-today">—</div></div>
      <div class="tile"><div class="k">Energia total</div><div class="v" id="t-total">—</div></div>
      <div class="tile"><div class="k">Painéis online</div><div class="v" id="t-online">—</div></div>
    </div>
    <div class="card">
      <dl>
        <dt>Estado</dt><dd id="state">…</dd>
        <dt>IP do DTU</dt><dd id="host">…</dd>
        <dt>Última leitura</dt><dd id="age">…</dd>
        <dt>Resposta do DTU</dt><dd id="fetchms">…</dd>
        <dt>Intervalo</dt><dd id="interval">…</dd>
        <dt>Nuvem Hoymiles</dt><dd id="cloud">…</dd>
        <dt>Último erro</dt><dd id="lasterr">…</dd>
        <dt>Busca automática</dt><dd id="autoscan">…</dd>
        <dt>MQTT</dt><dd id="mqtt">…</dd>
      </dl>
    </div>
    <p class="muted small">Atualiza sozinho a cada 5 s. À noite os painéis ficam offline e
      a potência em zero — é o normal do DTU.</p>
  </section>

  <!-- Paineis -->
  <section data-panel="paineis" hidden>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Painel</th><th>Potência (W)</th><th>Tensão PV (V)</th><th>Rede (V)</th>
          <th>Freq. (Hz)</th><th>Hoje (Wh)</th><th>Temp. (°C)</th><th>Atualizado</th>
        </tr></thead>
        <tbody id="panels"><tr><td colspan="8" class="muted">Sem leitura ainda.</td></tr></tbody>
      </table>
    </div>
  </section>

  <!-- Testar API -->
  <section data-panel="api" hidden>
    <div class="card">
      <label for="ep">Rota</label>
      <div class="row">
        <select id="ep"></select>
        <button id="send">Enviar</button>
      </div>
      <div class="url"><code id="fullurl"></code><button class="ghost" id="copyurl">Copiar</button></div>
      <div class="url"><code id="curl"></code><button class="ghost" id="copycurl">Copiar</button></div>
      <p class="small muted" id="urlnote"></p>
      <div class="resp-meta" id="respmeta"></div>
      <pre id="resp" hidden></pre>
    </div>
  </section>

  <!-- IP do DTU -->
  <section data-panel="ip" hidden>
    <form class="card" id="form">
      <label for="newhost">Novo IP do DTU</label>
      <div class="row">
        <input type="text" id="newhost" placeholder="192.168.0.34" autocomplete="off"
               spellcheck="false" inputmode="url" required>
        <button type="submit" id="save">Salvar</button>
      </div>
      <label class="check"><input type="checkbox" id="force">
        Salvar mesmo se o DTU não responder nesse IP</label>
      <div class="msg" id="msg"></div>
    </form>
    <div class="card">
      <label>Procurar DTU na rede</label>
      <p class="muted small" style="margin:0 0 10px">Testa todos os IPs da rede do Home
        Assistant atrás do DTU. Leva de 5 a 15 segundos.</p>
      <button id="scan">Procurar</button>
      <div class="msg" id="scanmsg"></div>
      <div id="scanres"></div>
    </div>
    <p class="muted small" id="persist"></p>
  </section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 0) => v == null ? "—" :
  Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
let cfg = null, lastPanels = [];

// ---- abas
function showTab(name) {
  document.querySelectorAll("nav button").forEach(b =>
    b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll("[data-panel]").forEach(p => p.hidden = p.dataset.panel !== name);
  try { localStorage.setItem("dtu-tab", name); } catch (e) {}
}
document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
let startTab = "coleta";
try { startTab = localStorage.getItem("dtu-tab") || "coleta"; } catch (e) {}
showTab(document.querySelector('[data-panel="' + startTab + '"]') ? startTab : "coleta");

async function getJson(path, opts) {
  const r = await fetch(path, { cache: "no-store", ...(opts || {}) });
  return { status: r.status, body: await r.json() };
}

// ---- coleta + paineis
function render(meta, data) {
  $("host").textContent = meta.dtu_host;
  const fresh = data && !meta.stale;
  $("state").innerHTML = fresh ? '<span class="ok">coletando normalmente</span>'
    : data ? '<span class="warn">DTU sem responder — mostrando a última leitura boa</span>'
           : '<span class="bad">ainda sem leitura do DTU</span>';
  $("age").textContent = meta.fetched_at == null ? "nenhuma ainda"
    : "há " + fmt(meta.age_s, 1) + " s (" + new Date(meta.fetched_at).toLocaleTimeString("pt-BR") + ")";
  $("fetchms").textContent = meta.fetch_ms == null ? "—" : meta.fetch_ms + " ms";
  $("interval").textContent = "a cada " + meta.poll_interval_s + " s";
  $("lasterr").textContent = meta.last_error
    ? (meta.last_error_hint ? meta.last_error_hint + " — " : "") + meta.last_error : "nenhum";
  $("lasterr").className = meta.last_error ? "bad" : "";
  const failing = meta.last_error && (meta.consecutive_failures >= 2 || !data);
  $("alert").hidden = !failing;
  if (failing) {
    $("a-host").textContent = meta.dtu_host;
    $("a-hint").textContent = (meta.last_error_hint || meta.last_error) +
      (meta.auto_discover ? ". A busca automática procura o DTU na rede depois de 3 falhas seguidas." : ".");
  }
  const mq = meta.mqtt;
  $("mqtt").className = mq && mq.enabled && !mq.connected ? "bad" : "";
  $("mqtt").textContent = !mq || !mq.enabled ? "desligado (aba Configuração do add-on)"
    : mq.connected ? "conectado a " + mq.broker + " · " + mq.entities + " sensores no Home Assistant" +
      (mq.last_publish ? " · último envio " + new Date(mq.last_publish).toLocaleTimeString("pt-BR") : "")
    : (mq.error || "conectando…") + (mq.broker ? " (" + mq.broker + ")" : "");
  const as = meta.last_auto_scan;
  $("autoscan").textContent = !meta.auto_discover ? "desligada (aba Configuração do add-on)"
    : !as ? "ligada — ainda não foi necessária"
    : "última em " + new Date(as.at).toLocaleTimeString("pt-BR") + ": " +
      (as.switched_to ? "achou e trocou para " + as.switched_to
       : as.found.length > 1 ? as.found.length + " DTUs encontrados — escolha na aba IP do DTU"
       : as.found.length === 1 ? "o DTU está no mesmo IP"
       : "nenhum DTU em " + (as.subnets.join(", ") || "nenhuma rede"));
  if (!data) return;
  const s = data.summary;
  $("t-power").innerHTML = fmt(s.p_total_kw, 2) + "<small>kW</small>";
  $("t-today").innerHTML = fmt(s.e_today_kwh, 2) + "<small>kWh</small>";
  $("t-total").innerHTML = fmt(s.e_total_kwh, 0) + "<small>kWh</small>";
  $("t-online").innerHTML = fmt(s.panels_online) + "<small>de " + fmt(s.panel_count) + "</small>";
  $("cloud").textContent = (s.server_status || "—") + " · " + (s.server_address || "?") +
    (s.server_port ? ":" + s.server_port : "");
  lastPanels = data.panels;
  const tb = $("panels");
  tb.textContent = "";
  if (!data.panels.length) {
    tb.innerHTML = '<tr><td colspan="8" class="muted">O DTU não listou painéis.</td></tr>';
  }
  for (const inv of data.inverters) {
    const tr = tb.insertRow(); tr.className = "inv";
    const c = tr.insertCell(); c.colSpan = 8;
    c.textContent = "Microinversor " + inv.serial + " · " + fmt(inv.p_grid, 1) + " W · " +
      fmt(inv.e_today_wh, 0) + " Wh hoje" + (inv.temperature != null ? " · " + fmt(inv.temperature, 1) + " °C" : "");
    for (const p of data.panels.filter(p => p.inverter_serial === inv.serial)) {
      const r = tb.insertRow();
      const name = r.insertCell();
      const dot = document.createElement("span"); dot.className = "dot" + (p.online ? " on" : "");
      dot.title = p.online ? "online" : "offline";
      name.append(dot, document.createTextNode(p.id));
      for (const [v, d] of [[p.p_grid, 1], [p.v_pv, 1], [p.v_grid, 1], [p.f_grid, 2],
                            [p.e_today_wh, 0], [p.temperature, 1]]) {
        r.insertCell().textContent = fmt(v, d);
      }
      r.insertCell().textContent = p.last_update ? p.last_update.replace("T", " ") : "—";
    }
  }
  fillEndpoints();
}

async function refresh() {
  try {
    const st = await getJson("api/status");
    if (st.status === 200) render(st.body.meta, st.body);
    else render(st.body.meta || (await getJson("health")).body, null);
  } catch (e) {
    $("state").innerHTML = '<span class="bad">add-on inacessível</span>';
  }
}

$("readnow").addEventListener("click", async () => {
  const b = $("readnow"); b.disabled = true; b.textContent = "Lendo…";
  try {
    const r = await getJson("api/refresh", { method: "POST" });
    render(r.body.meta, r.body.summary ? r.body : null);
    b.textContent = r.status === 200 ? "Lido ✓" : "Falhou";
  } catch (e) { b.textContent = "Falhou"; }
  setTimeout(() => { b.disabled = false; b.textContent = "Ler agora"; }, 1200);
});

// ---- testar API
function apiBase() {
  const port = cfg && cfg.api_port;
  return port ? location.protocol.replace("https:", "http:") + "//" + location.hostname + ":" + port : null;
}
let epFilled = "";
function fillEndpoints() {
  const first = lastPanels.length ? lastPanels[0].id : "100000000001-1";
  if (epFilled === first) return;
  const sel = $("ep"), cur = sel.value;
  sel.textContent = "";
  for (const p of ["/api/status", "/api/summary", "/api/panels", "/api/panels/" + first,
                   "/api/inverters", "/api/config", "/health"]) {
    sel.add(new Option("GET " + p, p));
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  epFilled = first;
  updateUrl();
}
function updateUrl() {
  const base = apiBase(), path = $("ep").value;
  $("fullurl").textContent = base ? base + path : "porta da API desativada";
  $("curl").textContent = base ? "curl -s " + base + path : "—";
  $("urlnote").innerHTML = base
    ? "Este é o endereço que o seu sistema deve chamar (rede local, sem login). " +
      "Se o HA estiver sendo acessado por outro nome/IP, troque o host pelo IP do Home Assistant."
    : '<span class="warn">A porta 8099 está desativada na seção Rede do add-on: ' +
      "o painel funciona, mas o seu sistema não vai conseguir chamar a API.</span>";
}
$("ep").addEventListener("change", updateUrl);

function highlight(json) {
  const esc = json.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc.replace(/("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)/g, (m) => {
    let c = "j-n";
    if (m[0] === '"') c = /:$/.test(m) ? "j-k" : "j-s";
    else if (/true|false|null/.test(m)) c = "j-b";
    return '<span class="' + c + '">' + m + "</span>";
  });
}
$("send").addEventListener("click", async () => {
  const path = $("ep").value, b = $("send");
  b.disabled = true;
  const t0 = performance.now();
  try {
    const r = await fetch(path.slice(1), { cache: "no-store" });
    const text = await r.text();
    const ms = Math.round(performance.now() - t0);
    let pretty = text;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (e) {}
    $("respmeta").innerHTML = '<span class="' + (r.ok ? "ok" : "bad") + '"><b>HTTP ' + r.status +
      "</b></span><span class=\"muted\">" + ms + " ms</span><span class=\"muted\">" +
      fmt(new Blob([text]).size) + " bytes</span>";
    $("resp").innerHTML = highlight(pretty);
    $("resp").hidden = false;
  } catch (e) {
    $("respmeta").innerHTML = '<span class="bad">Falha: ' + String(e).replace(/</g, "&lt;") + "</span>";
  } finally { b.disabled = false; }
});

function copy(text, btn) {
  const done = () => { btn.textContent = "Copiado"; setTimeout(() => btn.textContent = "Copiar", 1200); };
  const fallback = () => {
    const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta);
    ta.select(); try { document.execCommand("copy"); done(); } catch (e) {} ta.remove();
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, fallback);
  else fallback();
}
$("copyurl").addEventListener("click", (e) => copy($("fullurl").textContent, e.target));
$("copycurl").addEventListener("click", (e) => copy($("curl").textContent, e.target));

// ---- IP do DTU
async function loadConfig() {
  cfg = (await getJson("api/config")).body;
  $("ver").textContent = "v" + cfg.version;
  if (!$("newhost").value) $("newhost").value = cfg.dtu_host;
  $("persist").textContent = cfg.persistent
    ? "A troca é aplicada na hora e fica salva na aba Configuração do add-on."
    : "Rodando fora do Home Assistant: a troca vale até o processo reiniciar.";
  fillEndpoints(); updateUrl();
}
$("form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const msg = $("msg");
  $("save").disabled = true;
  msg.className = "msg muted"; msg.textContent = "Testando o DTU…";
  try {
    const r = await fetch("api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dtu_host: $("newhost").value, force: $("force").checked }),
    });
    const b = await r.json();
    if (r.ok) {
      let t = "IP trocado para " + b.dtu_host + ".";
      if (!b.reachable) t += " Atenção: o DTU não respondeu nesse IP.";
      if (!b.persisted) t += " Não foi possível salvar nas opções (" + b.persist_error + ").";
      msg.className = "msg " + (b.reachable && b.persisted ? "ok" : "bad");
      msg.textContent = t;
      $("force").checked = false;
    } else {
      msg.className = "msg bad";
      msg.textContent = r.status === 502
        ? "O DTU não respondeu em " + $("newhost").value.trim() +
          (b.reason ? ": " + b.reason : "") +
          ". Use a busca abaixo ou marque a opção acima para salvar assim mesmo."
        : b.error;
    }
  } catch (e) {
    msg.className = "msg bad"; msg.textContent = "Falha ao falar com o add-on: " + e;
  } finally {
    $("save").disabled = false;
    refresh();
  }
});

async function setHost(host, btn) {
  btn.disabled = true; btn.textContent = "Trocando…";
  try {
    const r = await getJson("api/config", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dtu_host: host }) });
    btn.textContent = r.status === 200 ? "Em uso ✓" : "Falhou";
    if (r.status === 200) { $("newhost").value = host; refresh(); }
  } catch (e) { btn.textContent = "Falhou"; btn.disabled = false; }
}
async function scan() {
  const b = $("scan"), msg = $("scanmsg"), res = $("scanres");
  b.disabled = true; b.textContent = "Procurando…";
  msg.className = "msg muted"; msg.textContent = "Testando os IPs da rede…"; res.textContent = "";
  try {
    const r = (await getJson("api/discover", { method: "POST" })).body;
    const nets = r.subnets.join(", ") || "nenhuma rede encontrada";
    if (!r.found.length) {
      msg.className = "msg bad";
      msg.textContent = "Nenhum DTU encontrado em " + nets + " (" + r.scanned + " IPs, " +
        r.elapsed_s + " s). Confira se o DTU está ligado e no mesmo Wi-Fi/rede do Home Assistant.";
    } else {
      msg.className = "msg ok";
      msg.textContent = (r.found.length === 1 ? "1 DTU encontrado" : r.found.length + " DTUs encontrados") +
        " em " + nets + " (" + r.elapsed_s + " s).";
    }
    for (const f of r.found) {
      const row = document.createElement("div"); row.className = "found";
      const info = document.createElement("div");
      const code = document.createElement("code"); code.textContent = f.host;
      const det = document.createElement("div"); det.className = "muted small";
      det.textContent = f.panel_count + " painéis · " + fmt(f.p_total_kw, 2) + " kW agora · " +
        fmt(f.e_total_kwh, 0) + " kWh total";
      info.append(code, det);
      const use = document.createElement("button");
      const current = cfg && f.host === $("host").textContent;
      use.textContent = current ? "Em uso" : "Usar este IP"; use.disabled = current;
      use.addEventListener("click", () => setHost(f.host, use));
      row.append(info, use); res.append(row);
    }
  } catch (e) {
    msg.className = "msg bad"; msg.textContent = "Falha na busca: " + e;
  } finally { b.disabled = false; b.textContent = "Procurar"; }
}
$("scan").addEventListener("click", scan);
$("a-scan").addEventListener("click", () => { showTab("ip"); scan(); });

loadConfig().catch(() => {}); refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def make_handler(collector):
    config_lock = threading.Lock()  # uma troca de IP por vez

    class Handler(BaseHTTPRequestHandler):
        server_version = "HoymilesDtuApi/" + VERSION

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send_raw(code, body, "application/json; charset=utf-8")

        def _send_raw(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _path(self):
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def _config(self):
            return {**collector.options(),
                    "persistent": bool(os.environ.get("SUPERVISOR_TOKEN")),
                    "api_port": external_api_port(self.server.server_port),
                    "version": VERSION}

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self._path()
            data, meta = collector.snapshot()

            if path == "/":
                # navegador (e o painel do add-on no HA) recebe a pagina; o resto, JSON
                if "text/html" in (self.headers.get("Accept") or ""):
                    return self._send_raw(200, PAGE_HTML.encode("utf-8"),
                                          "text/html; charset=utf-8")
                return self._send(200, {"name": "Hoymiles DTU API", "version": VERSION,
                                        "endpoints": ENDPOINTS})
            if path == "/health":
                return self._send(200, {"ok": data is not None and not meta["stale"], **meta})
            if path == "/api/config":
                return self._send(200, self._config())
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

        def do_POST(self):
            if self._path() == "/api/discover":
                return self._send(200, scan_for_dtus(collector.host, collector.timeout))
            if self._path() == "/api/refresh":
                collector.poll_once()
                data, meta = collector.snapshot()
                code = 200 if meta["last_error"] is None and data is not None else 502
                return self._send(code, {"meta": meta, **(data or {})})
            if self._path() != "/api/config":
                return self._send(404, {"error": "rota nao encontrada", "endpoints": ENDPOINTS})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self._send(413, {"error": "corpo ausente ou grande demais"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "envie JSON: {\"dtu_host\": \"192.168.0.34\"}"})

            host = normalize_host(body.get("dtu_host") if isinstance(body.get("dtu_host"), str) else "")
            if not host:
                return self._send(400, {"error": "dtu_host invalido; use IP ou nome, ex.: 192.168.0.34"})
            force = body.get("force") is True

            with config_lock:
                # testa o novo endereco antes de trocar, para nao perder a coleta por um IP errado
                data, test_error, test_hint = None, None, None
                try:
                    data = parse_dtu(fetch_hometable(host, collector.timeout))
                except Exception as exc:  # noqa: BLE001
                    test_error = "%s: %s" % (type(exc).__name__, exc)
                    test_hint = explain_error(exc, collector.timeout)
                if test_error and not force:
                    return self._send(502, {
                        "error": "o DTU nao respondeu em %s" % host,
                        "detail": test_error,
                        "reason": test_hint,
                        "hint": "envie \"force\": true para trocar mesmo assim",
                    })
                collector.set_host(host, data)
                persisted, persist_error = persist_options(collector.options())
            if not persisted:
                print("[api] IP trocado, mas nao salvo nas opcoes: %s" % persist_error, flush=True)
            return self._send(200, {
                "ok": True,
                "dtu_host": host,
                "reachable": test_error is None,
                "test_error": test_error,
                "persisted": persisted,
                "persist_error": persist_error,
            })

        def log_message(self, fmt, *args):  # silencia o log por requisicao
            pass

    return Handler


def main():
    opts = load_options()
    core = ("dtu_host", "poll_interval", "timeout", "auto_discover", "port")
    collector = Collector(opts["dtu_host"], opts["poll_interval"], opts["timeout"],
                          opts.get("auto_discover", True),
                          {k: v for k, v in opts.items() if k not in core})
    collector.mqtt = MqttBridge(collector, opts)
    threading.Thread(target=collector.run_forever, daemon=True).start()
    collector.mqtt.start()
    server = ThreadingHTTPServer(("0.0.0.0", opts["port"]), make_handler(collector))
    print("[api] v%s - DTU em %s, coleta a cada %ss, busca automatica %s, porta %s"
          % (VERSION, opts["dtu_host"], collector.interval,
             "ligada" if collector.auto_discover else "desligada", opts["port"]), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
