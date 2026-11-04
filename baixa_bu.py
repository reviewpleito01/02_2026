#!/usr/bin/env python3
"""Baixa os boletins de urna (bu.dat) dos dois turnos de uma zona, direto do TSE.

Uso:
  baixa_bu.py --uf sp --mun 71072 --zona 20 [--secoes 1-467 | --secoes-de logs/logs_saopaulo_20]
              [--turnos 3220,3221] [--pausa 0.5]
Saída: resultados/bu/<uf>_<mun>_<zona>/<pleito>/<seção>-<hash8>-<nome>.
Arquivos já baixados são pulados, então dá para interromper e rodar de novo.
Pleitos: 3220 = 1º turno (04/10), 3221 = 2º turno (25/10).
"""
import argparse, re, sys, time
from pathlib import Path
import requests

BASE = 'https://resultados.tse.jus.br/oficial/ele2026/arquivo-urna'
RAIZ = Path(__file__).resolve().parent.parent / 'resultados' / 'bu'

ap = argparse.ArgumentParser()
ap.add_argument('--uf', required=True)
ap.add_argument('--mun', required=True)
ap.add_argument('--zona', required=True, type=int)
ap.add_argument('--secoes', help='intervalo, ex. 1-467 ou 1,5,9')
ap.add_argument('--secoes-de', help='pasta logs_* de onde tirar a lista de seções (nomes NNNN-*.jez)')
ap.add_argument('--turnos', default='3220,3221')
ap.add_argument('--pausa', type=float, default=0.5)
a = ap.parse_args()

uf, mun, zona = a.uf.lower(), a.mun, f'{a.zona:04d}'
if a.secoes_de:
    secoes = sorted({int(p.name[:4]) for p in Path(a.secoes_de).iterdir() if re.match(r'\d{4}-', p.name)})
elif a.secoes:
    secoes = []
    for parte in a.secoes.split(','):
        ini, _, fim = parte.partition('-')
        secoes += range(int(ini), int(fim or ini) + 1)
else:
    sys.exit('informe --secoes ou --secoes-de')

s = requests.Session()
s.headers['User-Agent'] = 'Mozilla/5.0'
ok = pul = falta = erro = 0
for pleito in a.turnos.split(','):
    dest = RAIZ / f'{uf}_{mun}_{zona}' / pleito
    dest.mkdir(parents=True, exist_ok=True)
    for n in secoes:
        sec = f'{n:04d}'
        if any(dest.glob(f'{sec}-*-bu.dat')):
            pul += 1
            continue
        base = f'{BASE}/{pleito}/dados/{uf}/{mun}/{zona}/{sec}'
        try:
            r = s.get(f'{base}/p00{pleito}-{uf}-m{mun}-z{zona}-s{sec}-aux.json', timeout=30)
            if r.status_code == 404:
                falta += 1
                print(f'{pleito} {sec}: sem aux.json (404)')
                continue
            r.raise_for_status()
            achou = 0
            for g in r.json().get('hashes', []):
                tok = g.get('hash')
                for arq in g.get('arq', []):
                    nm = arq.get('nm', '')
                    if arq.get('tp') != 'bu' or not tok or '/' in nm:
                        continue
                    rb = s.get(f'{base}/{tok}/{nm}', timeout=60)
                    rb.raise_for_status()
                    if rb.content.lstrip().startswith(b'<'):
                        raise RuntimeError('resposta de erro do servidor')
                    (dest / f'{sec}-{tok[:8]}-{g.get("st","")}-{nm}').write_bytes(rb.content)
                    achou += 1
            ok += achou
            print(f'{pleito} {sec}: {achou} bu')
        except Exception as e:  # segue para a próxima seção
            erro += 1
            print(f'{pleito} {sec}: ERRO {e}')
        time.sleep(a.pausa)
print(f'\nbaixados {ok}, já existiam {pul}, sem aux {falta}, erros {erro} -> {RAIZ / f"{uf}_{mun}_{zona}"}')
