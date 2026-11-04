#!/usr/bin/env python3
"""
Varredura dos logs de urna: extrai os .jez (inclusive os de urnas trocadas), roda o forense.py em cada seção e gera
um relatório resumido por cidade e por zona, destacando as seções com anomalia.

É incremental: só extrai .jez novos e só roda o forense.py de novo quando o logd.dat
ou o próprio forense.py mudaram. Pode ser interrompido e retomado a qualquer momento.

Uso:
  python3 -I varredura.py                       # 25/10/2026, todas as pastas em pleito/logs
  python3 -I varredura.py --data 04/10/2026     # outro dia
  python3 -I varredura.py --lotes 'logs_saopaulo_*' --jobs 12
  python3 -I varredura.py --loop 30             # repete a cada 30 min (pega downloads novos)
  python3 -I varredura.py --pct-tecla 30        # alerta de tecla indevida acima de 30% dos votos

Rodar em segundo plano, sem depender do terminal:
  nohup python3 -I ~/pleito/ferramentas/varredura.py --loop 30 > ~/pleito/resultados/varredura.log 2>&1 &

Saída em pleito/resultados/varredura_<DDMMAAAA>/:
  relatorio.md            resumo por cidade/zona + seções em destaque
  secoes.csv              uma linha por seção, com todos os números e os alertas
  forense/<lote>/<seção>.txt  relatório do forense.py de cada seção (cache)
"""
import argparse
import csv
import glob
import heapq
import io
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

PLEITO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORENSE = os.path.join(PLEITO, "forense.py")
# Limite do alerta mesa→voto definido no forense.py (lido do arquivo para os textos do relatório)
LIMITE_MESA_VOTO = int(re.search(r"^LIMITE_MESA_VOTO = (\d+)", open(FORENSE, encoding="utf-8").read(), re.M).group(1))
LOGS = os.path.join(PLEITO, "logs")
RESULTADOS = os.path.join(PLEITO, "resultados")

# o03221 + UF (2) + município (5) + zona (4) + seção (4), ex.: o03221ba3849000010004
RE_ID = re.compile(r"o\d{5}([a-z]{2})(\d{5})(\d{4})(\d{4})")

# Limites dos alertas (ajustáveis)
IQR_FATOR = 3.0          # taxa da seção acima de Q3 + 3×IQR das taxas das seções da zona
MIN_CASOS = 10           # mínimo de casos para alertar taxa alta
DIG_NOVA_MIN = 10        # autorizações por digital desconhecida, cada uma de uma digital distinta
SEQ_MANUAL_MIN = 5       # eleitores manuais seguidos (mínimo absoluto)
P_SEQ_MAX = 0.001        # chance máxima de a sequência sair por acaso, dada a taxa de manuais da seção
ULTRA_MIN = 10           # votos < 10 s (além de fora da curva da zona)
MESA_MIN = 3             # votos mesa→voto abaixo do limite do forense.py (além de fora da curva da zona)


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ----------------------------------------------------------------------------- extração

def extrair_trocadas(z, destino):
    """Grava em <destino>/trocadas/<nome>.dat o logd.dat de cada .jez interno (urna trocada),
    vindo do .jez da seção e de .jez internos soltos em <destino> (extrações antigas completas)."""
    pasta = os.path.join(destino, "trocadas")
    fontes = {} if z is None else {os.path.basename(n): (lambda n=n: z.read(n))
                                   for n in z.namelist() if n.lower().endswith(".jez")}
    for solto in glob.glob(os.path.join(destino, "*.jez")):
        fontes.setdefault(os.path.basename(solto), lambda s=solto: open(s, "rb").read())
    for nome, ler in fontes.items():
        try:
            with zipfile.ZipFile(io.BytesIO(ler())) as zi:
                if "logd.dat" not in zi.namelist():
                    continue
                os.makedirs(pasta, exist_ok=True)
                with open(os.path.join(pasta, nome[:-4] + ".dat"), "wb") as dst:
                    dst.write(zi.read("logd.dat"))
        except (zipfile.BadZipFile, OSError):
            pass
    open(os.path.join(destino, ".trocadas_ok"), "w").close()


def extrair(lote_dir):
    """Extrai logd.dat de cada .jez para plain/<nome>/ e os das urnas trocadas para
    plain/<nome>/trocadas/. Retorna (novos, com_erro)."""
    plain = os.path.join(lote_dir, "plain")
    novos, erros = 0, []
    for jez in sorted(glob.glob(os.path.join(lote_dir, "*.jez"))):
        destino = os.path.join(plain, os.path.basename(jez)[:-4])
        tem_logd = os.path.exists(os.path.join(destino, "logd.dat"))
        if tem_logd and os.path.exists(os.path.join(destino, ".trocadas_ok")):
            continue
        if time.time() - os.path.getmtime(jez) < 60:   # provavelmente ainda baixando
            continue
        try:
            with zipfile.ZipFile(jez) as z:
                if not tem_logd:
                    if "logd.dat" not in z.namelist() or z.testzip() is not None:
                        raise zipfile.BadZipFile("sem logd.dat ou corrompido")
                    os.makedirs(destino, exist_ok=True)
                    with z.open("logd.dat") as src, open(os.path.join(destino, "logd.dat"), "wb") as dst:
                        dst.write(src.read())
                    novos += 1
                extrair_trocadas(z, destino)
        except (zipfile.BadZipFile, OSError) as e:
            erros.append(f"{os.path.basename(jez)} ({e})")
    # Seções já extraídas sem o .jez ao lado: usa só os .jez internos soltos na pasta
    for destino in glob.glob(os.path.join(plain, "*", "")):
        destino = destino.rstrip(os.sep)
        if (os.path.exists(os.path.join(destino, "logd.dat"))
                and not os.path.exists(os.path.join(destino, ".trocadas_ok"))
                and not os.path.exists(os.path.join(lote_dir, os.path.basename(destino) + ".jez"))):
            extrair_trocadas(None, destino)
    return novos, erros


# ----------------------------------------------------------------------------- forense

def rodar_forense(logd, trocadas, saida, data, tmpdir):
    """Roda o forense.py (log da seção + urnas trocadas) se o relatório não existe ou está desatualizado."""
    if os.path.exists(saida):
        t = os.path.getmtime(saida)
        if all(t >= os.path.getmtime(a) for a in (logd, FORENSE, *trocadas)):
            return False
    cmd = [sys.executable, "-I", FORENSE, logd, data]
    for a in trocadas:
        cmd += ["--urna-trocada", a]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpdir)
    tmp = saida + ".tmp"
    with open(tmp, "w") as f:
        f.write(r.stdout + r.stderr)
    os.replace(tmp, saida)
    return True


RE_DATA_LOG = re.compile(rb"^(\d{2})/(\d{2})/(\d{4}) ", re.M)
RE_TURNO_LOG = re.compile(rb"Iniciando aplica.{1,4}o - (\d).{1,3} turno")


def motivo_sem_votos(logs, linhas_dia):
    """Para seção sem voto computado no dia: diz se o log é só do 1º turno, se não tem
    nenhuma linha no dia ou se tem linhas no dia mas nenhum voto computado."""
    if linhas_dia:
        return "linhas no dia, nenhum voto computado"
    b = b"".join(open(a, "rb").read() for a in logs)
    turnos = set(RE_TURNO_LOG.findall(b))
    datas = {(a, m, d) for d, m, a in RE_DATA_LOG.findall(b)}
    ultima = "/".join(x.decode() for x in reversed(max(datas))) if datas else "sem datas"
    if turnos == {b"1"}:
        return f"log do 1º turno (última data {ultima})"
    return f"sem linhas no dia (última data {ultima})"


def num(padrao, texto, grupo=1):
    m = re.search(padrao, texto, re.M)
    return int(m.group(grupo)) if m else 0


def ler_relatorio(caminho):
    t = open(caminho, encoding="utf-8", errors="ignore").read()
    d = {
        "votos": num(r"Total de eleitores validados: (\d+)", t),
        "sem_ciclos": "Nenhum ciclo completo" in t,
        "urnas_trocadas": num(r"Urnas trocadas incluídas\s*: (\d+)", t),
        "linhas_dia": num(r"\(no dia: (\d+)\)", t),
        "manuais": num(r"Total Habilitações Manuais\s*: (\d+)", t),
        "falha_bio": num(r"Com Falha/Timeout de Biometria: (\d+)", t),
        "ano_nasc": num(r"\(Ano de Nascimento\): (\d+)", t),
        "mes_cad": num(r"Mesário Cadastrado\s*: (\d+)", t),
        "mesarios_distintos": num(r"Mesário Cadastrado\s*: \d+ \((\d+) mes", t),
        "dig_desc": num(r"Digital Desconhecida\s*: (\d+)", t),
        "dig_distintas": num(r"Digital Desconhecida\s*: \d+ \((\d+) digitais", t),
        "seq_manual": num(r"Maior Sequência Manual\s*: (\d+)", t),
        "tecla_indevida": num(r"Votos com Tecla Indevida\s*: (\d+)", t),
        "mesa_voto_rapido": num(r"Votos Mesa→Voto < \d+s\s*: (\d+)", t),
        "ultrarrapidos": len(re.findall(r"\| Votação Ultrarrápida", t)),
        "transicao_imediata": len(re.findall(r"\| Transição Imediata", t)),
        "mediana_s": 0.0,
    }
    m = re.search(r"Mediana do Tempo\s*: ([\d.]+)", t)
    if m:
        d["mediana_s"] = float(m.group(1))
    # Tabela de 15 em 15 minutos: " HH:MM        | votos | manuais | ..."
    janelas = {k: (int(v), int(mn)) for k, v, mn in
               re.findall(r"^ (\d{2}:\d{2}) +\| (\d+) +\| (\d+)", t, re.M)}
    d["janelas"] = janelas
    d["votos_1715"] = sum(v for k, (v, _) in janelas.items() if k >= "17:15")
    d["votos_19h"] = sum(v for k, (v, _) in janelas.items() if k >= "19:00")
    d["ultimo_voto_janela"] = max((k for k, (v, _) in janelas.items() if v), default="")
    return d


# ----------------------------------------------------------------------------- alertas

TAXAS = {"ano": "ano_nasc", "falha": "falha_bio", "ultra": "ultrarrapidos", "mesa": "mesa_voto_rapido"}


def limites_zona(lista):
    """Taxa por voto acima da qual a seção fica fora da curva da zona (Q3 + IQR_FATOR × IQR).
    Usa a distribuição das seções, não a binomial: as seções já variam muito entre si."""
    lim = {}
    validas = [s for s in lista if s["votos"]]
    for nome, campo in TAXAS.items():
        taxas = sorted(s[campo] / s["votos"] for s in validas)
        if len(taxas) < 4:
            lim[nome] = (float("inf"), 0.0)
            continue
        q1, _, q3 = statistics.quantiles(taxas, n=4)
        lim[nome] = (q3 + IQR_FATOR * (q3 - q1), statistics.median(taxas))
    return lim


def chance_sequencia(k, n, p):
    """Chance aproximada de haver, ao acaso, k ou mais manuais seguidos em n eleitores,
    quando cada um é manual com probabilidade p (independentes)."""
    if k <= 0 or p <= 0:
        return 1.0
    if p >= 1:
        return 1.0
    return 1 - math.exp(-((n - k + 1) * (1 - p) + 1) * p ** k)


def alertas(sec, lim, pct_tecla):
    a = []
    if sec["sem_ciclos"]:
        return [f"sem votos no dia: {sec['motivo_sem_votos']}"]
    n = sec["votos"]
    for nome, campo, rotulo, minimo in (("ano", "ano_nasc", "ano de nascimento alto", MIN_CASOS),
                                        ("falha", "falha_bio", "falha da digital alta", MIN_CASOS),
                                        ("ultra", "ultrarrapidos", "votos <10 s acima da zona", ULTRA_MIN),
                                        ("mesa", "mesa_voto_rapido", f"mesa→voto <{LIMITE_MESA_VOTO} s acima da zona", MESA_MIN)):
        teto, mediana = lim[nome]
        if sec[campo] >= minimo and sec[campo] / n > teto:
            a.append(f"{rotulo} ({sec[campo]}, mediana da zona daria {n * mediana:.0f})")
    # Tecla indevida: limite fixo sobre os votos computados na seção (não compara com a zona)
    if sec["tecla_indevida"] / n > pct_tecla / 100:
        a.append(f"tecla indevida em {sec['tecla_indevida']} de {n} votos ({sec['tecla_indevida'] / n * 100:.1f}%)")
    # Só quando a falha da digital também está fora da curva da zona: autorizador nunca
    # reconhecido sozinho é comum (mesários não cadastrados ou com leitura ruim)
    if (sec["dig_distintas"] >= DIG_NOVA_MIN and sec["dig_distintas"] == sec["dig_desc"]
            and sec["falha_bio"] / n > lim["falha"][0]):
        a.append(f"autorizador nunca reconhecido ({sec['dig_desc']} autorizações, cada uma de uma digital distinta)")
    p_seq = chance_sequencia(sec["seq_manual"], n, sec["manuais"] / n)
    if sec["seq_manual"] >= SEQ_MANUAL_MIN and p_seq < P_SEQ_MAX:
        a.append(f"{sec['seq_manual']} manuais seguidos (taxa de manuais da seção {sec['manuais']}/{n}; "
                 f"chance ao acaso {p_seq:.0e})")
    if sec["votos_19h"]:
        a.append(f"{sec['votos_19h']} votos a partir de 19:00 (último bloco {sec['ultimo_voto_janela']})")
    j = sec["janelas"]
    tarde = max([v for k, (v, _) in j.items() if "15:00" <= k < "17:00"] or [0])
    pos = max([v for k, (v, _) in j.items() if k >= "17:15"] or [0])
    if pos >= max(5, tarde) and pos:
        a.append(f"pico após 17:15 ({pos} votos em 15 min; máximo 15h–17h: {tarde})")
    return a


# ----------------------------------------------------------------------------- varredura
#
# Memória: o índice guarda só (tamanho, caminho) por seção. Os relatórios são lidos e avaliados
# uma zona de cada vez; o CSV é escrito na hora e só ficam na memória os totais por cidade/zona/hora
# e as seções em destaque (no máximo --top por cidade).

def indexar(args):
    """Extrai os .jez novos e monta {(município, zona): {seção: (tamanho, logd)}}.
    Mesma seção em mais de um lote (ex.: pasta de teste): fica o maior logd.dat."""
    lotes = sorted(d for d in glob.glob(os.path.join(LOGS, args.lotes)) if os.path.isdir(d))
    indice, nomes_cidade, erros_ext, dup = defaultdict(dict), {}, {}, 0
    for lote_dir in lotes:
        lote = os.path.basename(lote_dir)
        novos, erros = extrair(lote_dir)
        if novos:
            log(f"{lote}: {novos} .jez extraídos")
        if erros:
            erros_ext[lote] = erros
        cidade_pasta = re.sub(r"_\d+$", "", re.sub(r"^logs_", "", lote))
        for logd in glob.iglob(os.path.join(lote_dir, "plain", "*", "logd.dat")):
            m = RE_ID.search(os.path.basename(os.path.dirname(logd)))
            if not m:
                continue
            _, mun, zona, secao = m.groups()
            nomes_cidade.setdefault(mun, defaultdict(int))[cidade_pasta] += 1
            tam = os.path.getsize(logd)
            atual = indice[(mun, zona)].get(secao)
            if atual:
                dup += 1
                if atual[0] >= tam:
                    continue
            indice[(mun, zona)][secao] = (tam, logd)
    # Nome da cidade: o nome de pasta mais comum para aquele código de município
    nome = {mun: max(c, key=c.get) for mun, c in nomes_cidade.items()}
    return indice, nome, erros_ext, dup


CHAVES_SOMA = ["votos", "manuais", "ano_nasc", "falha_bio", "mes_cad", "dig_desc", "dig_distintas",
               "votos_1715", "votos_19h", "tecla_indevida", "mesa_voto_rapido"]


def nova_soma():
    return dict.fromkeys(CHAVES_SOMA + ["secoes", "sem_votos", "sem_votos_1t", "com_trocadas", "sinalizadas", "acima_manual",
                                        "acima_digital"], 0)


def acima(s, campo, pct):
    return s["votos"] > 0 and s[campo] / s["votos"] > pct / 100


def somar(t, s, args):
    t["acima_manual"] += acima(s, "manuais", args.pct_manual)
    t["acima_digital"] += acima(s, "falha_bio", args.pct_digital)
    for k in CHAVES_SOMA:
        t[k] += s[k]
    t["secoes"] += 1
    t["sem_votos"] += s["sem_ciclos"]
    t["com_trocadas"] += bool(s["urnas_trocadas"])
    t["sem_votos_1t"] += s["motivo_sem_votos"].startswith("log do 1º turno")
    t["sinalizadas"] += bool(s["alertas"]) and not s["sem_ciclos"]


def varrer(args):
    data = args.data
    saida_base = os.path.join(RESULTADOS, "varredura_" + data.replace("/", ""))
    indice, nome, erros_ext, dup = indexar(args)
    total_secoes = sum(len(v) for v in indice.values())
    log(f"{total_secoes} seções em {len(indice)} zonas ({dup} duplicadas ignoradas); "
        f"forense.py com {args.jobs} processos")

    tot_cidade = defaultdict(nova_soma)
    tot_zona = defaultdict(nova_soma)
    horas = defaultdict(lambda: defaultdict(lambda: [0, 0]))   # cidade -> hora -> [votos, manuais]
    destaques = defaultdict(list)                              # cidade -> heap das seções com mais alertas
    sem_votos = defaultdict(list)
    lista_digital = []                                         # seções acima de --pct-digital
    feitos = vistos = 0

    csv_path = os.path.join(saida_base, "secoes.csv")
    os.makedirs(saida_base, exist_ok=True)
    zonas = sorted(indice, key=lambda k: (nome[k[0]], k[1]))
    with tempfile.TemporaryDirectory() as tmpdir, ThreadPoolExecutor(args.jobs) as ex, \
            open(csv_path + ".tmp", "w", newline="") as fcsv:
        w = csv.writer(fcsv)
        w.writerow(CAMPOS)
        for mun, zona in zonas:
            cidade = nome[mun]
            tarefas = []
            for secao, (_, logd) in sorted(indice.pop((mun, zona)).items()):
                nome_dir = os.path.basename(os.path.dirname(logd))
                lote = os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(logd))))
                rel = os.path.join(saida_base, "forense", lote, nome_dir + ".txt")
                trocadas = sorted(glob.glob(os.path.join(os.path.dirname(logd), "trocadas", "*.dat")))
                tarefas.append(dict(cidade=cidade, uf=RE_ID.search(nome_dir).group(1), mun=mun, zona=zona,
                                    secao=secao, lote=lote, logd=logd, trocadas=trocadas, rel=rel))
            for d in {os.path.dirname(t["rel"]) for t in tarefas}:
                os.makedirs(d, exist_ok=True)
            feitos += sum(ex.map(lambda t: rodar_forense(t["logd"], t["trocadas"], t["rel"], data, tmpdir), tarefas))

            secoes = []
            for t in tarefas:
                sec = ler_relatorio(t["rel"])
                sec.update({k: v for k, v in t.items() if k != "trocadas"})
                sec["motivo_sem_votos"] = motivo_sem_votos([t["logd"], *t["trocadas"]], sec["linhas_dia"]) if sec["sem_ciclos"] else ""
                secoes.append(sec)
            lim = limites_zona(secoes)
            for sec in secoes:
                sec["alertas"] = alertas(sec, lim, args.pct_tecla)
                w.writerow([" | ".join(sec[c]) if c == "alertas" else sec[c] for c in CAMPOS])
                somar(tot_cidade[cidade], sec, args)
                somar(tot_zona[(cidade, zona)], sec, args)
                if acima(sec, "falha_bio", args.pct_digital):
                    # falha_bio = liberações autorizadas pela digital do mesário (cadastrado ou não)
                    lista_digital.append((sec["falha_bio"] / sec["votos"], cidade, zona, sec["secao"], sec["votos"],
                                          sec["falha_bio"], sec["mes_cad"], sec["dig_desc"], sec["dig_distintas"],
                                          sec["ano_nasc"], sec["manuais"]))
                for k, (v, mn) in sec["janelas"].items():
                    h = horas[cidade][int(k[:2])]
                    h[0] += v
                    h[1] += mn
                if sec["sem_ciclos"]:
                    sem_votos[cidade].append((zona, sec["secao"], sec["lote"], sec["motivo_sem_votos"]))
                elif sec["alertas"]:
                    item = (len(sec["alertas"]), [-int(zona), -int(sec["secao"])],
                            (zona, sec["secao"], sec["votos"], sec["ano_nasc"], sec["falha_bio"],
                             "; ".join(sec["alertas"])))
                    if len(destaques[cidade]) < args.top:
                        heapq.heappush(destaques[cidade], item)
                    else:
                        heapq.heappushpop(destaques[cidade], item)
            vistos += len(secoes)
            del secoes, tarefas
            log(f"  {cidade} zona {zona}: {vistos}/{total_secoes} seções ({feitos} relatórios novos)")
    os.replace(csv_path + ".tmp", csv_path)
    log(f"forense.py executado em {feitos} seções (demais já estavam em cache)")

    lista_digital.sort(reverse=True)
    lista_path = os.path.join(saida_base, "secoes_digital_mesario.csv")
    with open(lista_path + ".tmp", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pct_digital_mesario", "cidade", "zona", "secao", "votos", "autoriz_digital_mesario",
                    "mesario_cadastrado", "digital_desconhecida", "digitais_distintas", "ano_nasc", "manuais"])
        for item in lista_digital:
            w.writerow([f"{item[0] * 100:.1f}", *item[1:]])
    os.replace(lista_path + ".tmp", lista_path)
    escrever_relatorio(tot_cidade, tot_zona, horas, destaques, sem_votos, lista_digital, erros_ext, dup, data,
                       os.path.join(saida_base, "relatorio.md"), args)
    log(f"relatório: {os.path.join(saida_base, 'relatorio.md')}")


# ----------------------------------------------------------------------------- saída

CAMPOS = ["cidade", "uf", "mun", "zona", "secao", "lote", "votos", "manuais", "ano_nasc", "falha_bio",
          "mes_cad", "mesarios_distintos", "dig_desc", "dig_distintas", "seq_manual", "tecla_indevida",
          "ultrarrapidos", "mesa_voto_rapido", "transicao_imediata", "mediana_s", "votos_1715", "votos_19h",
          "ultimo_voto_janela", "urnas_trocadas", "motivo_sem_votos", "alertas"]


def linha_tabela(rotulo, t):
    com_pct = lambda campo: (f"{t[campo]:,}".replace(",", ".") + " (" +
                             (f"{t[campo] / t['votos'] * 100:.1f}".replace(".", ",") if t["votos"] else "0,0") + "%)")
    return (f"| {rotulo} | {t['secoes']:,} | {t['votos']:,} | {t['ano_nasc']:,} | {t['falha_bio']:,} | "
            f"{t['mes_cad']:,} | {t['dig_desc']:,} | {t['votos_1715']:,} | "
            f"{t['votos_19h']:,} | {t['acima_manual']:,} | {t['acima_digital']:,} | "
            f"{t['sinalizadas']:,} | {t['sem_votos']:,} ({t['sem_votos_1t']:,}) |").replace(",", ".") + f" {com_pct('tecla_indevida')} | {com_pct('mesa_voto_rapido')} |"


CABECALHO = ("| {} | Seções | Votos | Ano nasc. | Falha digital | Autoriz. mesário cadastrado | "
             "Autoriz. digital desconhecida | Votos ≥17:15 | Votos ≥19:00 | Seções >{m}% manuais | "
             "Seções >{d}% digital do mesário | Seções c/ alerta | Seções sem votos (log do 1º turno) | "
             "Votos c/ tecla indevida | Votos mesa→voto <{l} s |\n"
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")


def escrever_relatorio(tot_cidade, tot_zona, horas, destaques, sem_votos, lista_digital, erros_ext, dup, data,
                       caminho, args):
    top = args.top
    cab = lambda rotulo: CABECALHO.format(rotulo, m=f"{args.pct_manual:g}", d=f"{args.pct_digital:g}", l=LIMITE_MESA_VOTO)
    cidades = sorted(tot_cidade)
    geral = nova_soma()
    for t in tot_cidade.values():
        for k in geral:
            geral[k] += t[k]
    L = [f"# Varredura dos logs de urna — {data}",
         "",
         f"Gerado em {datetime.now():%d/%m/%Y %H:%M}. Números do forense.py de cada seção "
         f"(votos ≥17:15 e ≥19:00 somados da tabela de 15 min do forense.py; alertas calculados por este script).",
         "",
         "## Por cidade", "", cab("Cidade")]
    for c in cidades:
        L.append(linha_tabela(c, tot_cidade[c]))
    L.append(linha_tabela("**Total**", geral))
    L += ["", f"Manuais = todas as liberações manuais (ano de nascimento + falha da digital). "
              f"Digital do mesário = liberações após falha da digital do eleitor, autorizadas pela digital "
              f"do mesário (cadastrado ou desconhecido). Tecla indevida = votos com pelo menos um "
              f"\"Tecla indevida pressionada\" entre \"Eleitor foi habilitado\" e o voto para Presidente. "
              f"Mesa→voto <{LIMITE_MESA_VOTO} s = votos computados menos de {LIMITE_MESA_VOTO} s depois da última etapa de "
              f"identificação na mesa (digital do eleitor ou do mesário capturada, dado pessoal conferido ou ano de "
              f"nascimento igual ao cadastro)."]

    todas_sem = [(c, *item) for c in cidades for item in sem_votos[c]]
    por_motivo = defaultdict(int)
    for *_, m in todas_sem:
        por_motivo[re.sub(r" \(.*", "", m)] += 1
    L += ["", f"## Seções sem votos registrados em {data} ({len(todas_sem)})", ""]
    if todas_sem:
        L += [f"- {m}: {q}" for m, q in sorted(por_motivo.items(), key=lambda x: -x[1])]
        L += ["", "| Cidade | Zona/Seção | Pasta | Motivo |", "|---|---|---|---|"]
        L += [f"| {c} | {z}/{s} | {lote} | {m} |" for c, z, s, lote, m in todas_sem]
        L += ["", "\"Log do 1º turno\" = o log não tem nenhuma linha no dia e só inicia a aplicação do "
                  "1º turno (arquivo provavelmente baixado do 1º turno)."]
    else:
        L.append("Nenhuma: todas as seções têm pelo menos um voto computado no dia.")

    L += ["", f"## Seções com mais de {args.pct_digital:g}% dos votos autorizados pela digital do mesário "
              f"({len(lista_digital)})", "",
          "Lista completa, da maior para a menor porcentagem (também em `secoes_digital_mesario.csv`).", "",
          "| % | Cidade | Zona/Seção | Votos | Autoriz. digital mesário | Mesário cadastrado | "
          "Digital desconhecida (distintas) | Ano nasc. |",
          "|---:|---|---|---:|---:|---:|---:|---:|"]
    for pct, cid, zona, secao, votos, dig, cad, desc, dist, ano, _ in lista_digital:
        L.append(f"| {pct * 100:.1f} | {cid} | {zona}/{secao} | {votos} | {dig} | {cad} | {desc} ({dist}) | {ano} |")

    # Liberações manuais por hora (o forense.py separa manual/total por janela, não por causa)
    L += ["", "## Votos e liberações manuais por hora", "",
          "| Cidade | " + " | ".join(f"{h}h" for h in range(7, 20)) + " |",
          "|---|" + "---:|" * 13]
    for c in cidades:
        L.append(f"| {c} | " + " | ".join(f"{horas[c][h][1]:,}/{horas[c][h][0]:,}".replace(",", ".")
                                          for h in range(7, 20)) + " |")
    L.append("\n(manuais/votos em cada hora; 7h inclui qualquer voto antes das 8h)")

    for c in cidades:
        L += ["", f"## {c}", "", cab("Zona")]
        for (cid, z) in sorted(k for k in tot_zona if k[0] == c):
            L.append(linha_tabela(z, tot_zona[(cid, z)]))
        if sem_votos[c]:
            L += ["", f"Seções sem votos no dia ({len(sem_votos[c])}): " +
                  ", ".join(f"{z}/{s} ({m})" for z, s, _, m in sem_votos[c])]
        sinal = [item[2] for item in sorted(destaques[c], reverse=True)]
        if sinal:
            L += ["", f"**Seções em destaque** ({tot_cidade[c]['sinalizadas']}; mostrando até {top}, "
                      "ordem: mais alertas primeiro; lista completa no secoes.csv)", "",
                  "| Zona/Seção | Votos | Ano nasc. | Falha | Alertas |", "|---|---:|---:|---:|---|"]
            for zona, secao, votos, ano, falha, txt in sinal:
                L.append(f"| {zona}/{secao} | {votos} | {ano} | {falha} | {txt} |")

    L += ["", "## Observações", "",
          f"- Seções duplicadas (mesma seção em mais de uma pasta) ignoradas: {dup}.",
          f"- Urnas trocadas: o logd.dat de cada `.jez` dentro do `.jez` da seção é somado ao da urna principal "
          f"(em ordem cronológica; cópias idênticas ignoradas). Seções com urna trocada incluída: "
          f"{geral['com_trocadas']:,}.".replace(",", "."),
          f"- Critérios dos alertas: tecla indevida em mais de {args.pct_tecla:g}% dos votos computados na seção; "
          f"taxa (por voto) de ano de nascimento, falha da digital, votos < 10 s ou mesa→voto < {LIMITE_MESA_VOTO} s "
          f"fora da curva da zona (acima de Q3 + {IQR_FATOR:g}×IQR das seções da zona, com ≥ {MIN_CASOS} casos; "
          f"≥ {ULTRA_MIN} para < 10 s; ≥ {MESA_MIN} para mesa→voto); ≥ {DIG_NOVA_MIN} autorizações por digital desconhecida, cada uma de "
          f"uma digital distinta, junto com falha da digital fora da curva; ≥ {SEQ_MANUAL_MIN} manuais seguidos "
          f"com chance ao acaso < {P_SEQ_MAX:g} dada a taxa de manuais da própria seção; votos a partir de 19:00; "
          "pico de votos após 17:15 maior ou igual ao maior das 15h–17h (e ≥ 5)."]
    if erros_ext:
        L += ["- `.jez` que não puderam ser extraídos (download incompleto? são tentados de novo na próxima execução):"]
        for lote, errs in erros_ext.items():
            L.append(f"  - {lote}: " + ", ".join(errs[:10]) + (f" … (+{len(errs) - 10})" if len(errs) > 10 else ""))

    with open(caminho + ".tmp", "w") as f:
        f.write("\n".join(L) + "\n")
    os.replace(caminho + ".tmp", caminho)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default="25/10/2026", help="dia analisado (DD/MM/AAAA)")
    p.add_argument("--lotes", default="logs_*", help="glob das pastas dentro de pleito/logs")
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="processos em paralelo")
    p.add_argument("--top", type=int, default=50, help="máximo de seções em destaque por cidade")
    p.add_argument("--pct-manual", type=float, default=20,
                   help="totalizar seções com mais que este %% dos votos com liberação manual")
    p.add_argument("--pct-digital", type=float, default=10,
                   help="listar seções com mais que este %% dos votos autorizados pela digital do mesário")
    p.add_argument("--pct-tecla", type=float, default=40,
                   help="alertar seções com tecla indevida em mais que este %% dos votos computados")
    p.add_argument("--loop", type=int, default=0, help="repetir a cada N minutos (0 = uma vez)")
    args = p.parse_args()
    while True:
        varrer(args)
        if not args.loop:
            break
        log(f"próxima varredura em {args.loop} min")
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
