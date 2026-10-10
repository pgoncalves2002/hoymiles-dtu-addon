#!/usr/bin/env python3
"""Hoymiles DTU -> API REST (JSON).

Consulta periodicamente http://<dtu>/hometable.xml (a mesma fonte usada pela
pagina home.htm do DTU) e expoe a ultima leitura em JSON.

O IP do DTU pode ser trocado em tempo real (painel do add-on no Home
Assistant ou POST /api/config); a troca e salva nas opcoes do add-on via
Supervisor, entao vale tambem depois de reiniciar.

Sem dependencias externas: apenas biblioteca padrao do Python 3.
"""
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPTIONS_FILE = "/data/options.json"  # gerado pelo Home Assistant Supervisor
SUPERVISOR_OPTIONS_URL = "http://supervisor/addons/self/options"
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"
HEADER_FIELDS = 12                   # campos antes da lista de paineis
ROW_FIELDS = 8                       # campos por painel
VERSION = "1.1.0"
# IP ou nome na rede, com porta opcional: 192.168.0.34 | dtu.lan | 192.168.0.34:8080
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?(?::\d{1,5})?$")


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
def fetch_hometable(host, timeout):
    url = "http://%s/hometable.xml?_t=%d" % (host, int(time.time() * 1000))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except http.client.IncompleteRead as exc:  # servidor embarcado simples
        raw = exc.partial
    return raw.decode("latin-1")


class Collector:
    def __init__(self, host, interval, timeout):
        self.host = host
        self.interval = max(1, int(interval))
        self.timeout = int(timeout)
        self._lock = threading.Lock()
        self._data = None
        self._fetched_at = None
        self._last_error = None
        self._last_attempt = None
        self._fetch_ms = None

    @property
    def url(self):
        return "http://%s/hometable.xml" % self.host

    def options(self):
        """Opcoes no formato do schema do add-on (config.yaml)."""
        return {"dtu_host": self.host, "poll_interval": self.interval,
                "timeout": self.timeout}

    def set_host(self, host, data=None):
        """Troca o DTU consultado sem reiniciar.

        `data` e a leitura de teste feita no novo endereco (se houver); sem
        ela, a leitura antiga e descartada para nao misturar os dois DTUs.
        """
        with self._lock:
            old, self.host = self.host, host
            self._data = data
            self._fetched_at = time.time() if data is not None else None
            self._last_error = None
        print("[coletor] DTU trocado: %s -> %s" % (old, host), flush=True)

    def poll_once(self):
        now = time.time()
        with self._lock:
            host = self.host
        try:
            data = parse_hometable(fetch_hometable(host, self.timeout))
            with self._lock:
                if host != self.host:  # o IP mudou durante a leitura
                    return
                self._data, self._fetched_at, self._last_error = data, now, None
                self._fetch_ms = round((time.time() - now) * 1000)
        except Exception as exc:  # noqa: BLE001 - mantem a ultima leitura boa
            with self._lock:
                if host != self.host:
                    return
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
            print("[coletor] falha ao ler %s: %s" % (host, exc), flush=True)
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
            host, fetch_ms = self.host, self._fetch_ms
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
        }
        return data, meta


# --------------------------------------------------------------------- API --
ENDPOINTS = {
    "/api/status": "leitura completa (resumo + paineis + microinversores)",
    "/api/summary": "totais do sistema",
    "/api/panels": "lista de paineis",
    "/api/panels/<id>": "um painel, ex.: /api/panels/100000000001-1",
    "/api/inverters": "paineis agrupados por microinversor",
    "/api/config": "GET: configuracao atual | POST {\"dtu_host\": \"...\"}: troca o IP do DTU",
    "/api/refresh": "POST: le o DTU agora e devolve a leitura (para testes)",
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
  $("lasterr").textContent = meta.last_error || "nenhum";
  $("lasterr").className = meta.last_error ? "bad" : "";
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
          ". Confira o IP ou marque a opção acima para salvar assim mesmo."
        : b.error;
    }
  } catch (e) {
    msg.className = "msg bad"; msg.textContent = "Falha ao falar com o add-on: " + e;
  } finally {
    $("save").disabled = false;
    refresh();
  }
});

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
                data, test_error = None, None
                try:
                    data = parse_hometable(fetch_hometable(host, collector.timeout))
                except Exception as exc:  # noqa: BLE001
                    test_error = "%s: %s" % (type(exc).__name__, exc)
                if test_error and not force:
                    return self._send(502, {
                        "error": "o DTU nao respondeu em %s" % host,
                        "detail": test_error,
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
    collector = Collector(opts["dtu_host"], opts["poll_interval"], opts["timeout"])
    threading.Thread(target=collector.run_forever, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", opts["port"]), make_handler(collector))
    print("[api] v%s - DTU em %s, coleta a cada %ss, ouvindo na porta %s"
          % (VERSION, opts["dtu_host"], collector.interval, opts["port"]), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
