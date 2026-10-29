
import time
from pathlib import Path

import requests

BASE = (
    "https://resultados.tse.jus.br/oficial/"
    "ele2026/arquivo-urna/3221"
)

UF = "sp"
MUNICIPIO = "71072"
ZONA = "0006"

DESTINO = Path("logs_saopaulo_06")
DESTINO.mkdir(parents=True, exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})


def baixar_log(secao: int) -> None:
    s = f"{secao:04d}"

    # 1. Consultar o JSON auxiliar da seção
    aux_url = (
        f"{BASE}/dados/{UF}/{MUNICIPIO}/{ZONA}/{s}/"
        f"p003221-{UF}-m{MUNICIPIO}-z{ZONA}-s{s}-aux.json"
    )

    print(f"\nSeção {s}: consultando auxiliar...")

    response = session.get(aux_url, timeout=30)
    response.raise_for_status()
    aux = response.json()

    # 2. Localizar os arquivos de log e seus respectivos hashes
    logs_encontrados = 0

    for grupo in aux.get("hashes", []):
        token = grupo.get("hash")

        if not token:
            continue

        for arquivo in grupo.get("arq", []):
            if arquivo.get("tp") != "log":
                continue

            nome = arquivo.get("nm")

            if not nome or "/" in nome or "\\" in nome:
                continue

            # 3. Montar a URL real do arquivo
            url = (
                f"{BASE}/dados/{UF}/{MUNICIPIO}/{ZONA}/{s}/"
                f"{token}/{nome}"
            )

            # Preservar arquivos de grupos diferentes
            # sem sobrescrever downloads anteriores.
            destino = DESTINO / f"{s}-{token[:8]}-{nome}"

            print(f"Baixando: {nome}")

            r = session.get(url, timeout=60)
            r.raise_for_status()

            # Detectar respostas XML de erro do armazenamento
            if r.content.lstrip().startswith(b"<Error>"):
                raise RuntimeError(
                    f"O servidor retornou erro para a seção {s}"
                )

            destino.write_bytes(r.content)

            print(
                f"OK: {destino} "
                f"({len(r.content)} bytes)"
            )

            logs_encontrados += 1

    if logs_encontrados == 0:
        print(f"Nenhum arquivo de log encontrado para a seção {s}.")


if __name__ == "__main__":
    # Exemplo: baixar as seções 1 e 2
    secoes = range(1, 468)

    for secao in secoes:
        try:
            baixar_log(secao)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"ERRO na seção {secao:04d}: {exc}")

        time.sleep(1)

    print("\nProcessamento concluído.")

