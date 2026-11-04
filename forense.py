import re
import sys
import os
import statistics
from datetime import datetime

# Votos com menos que isto entre a última etapa de identificação na mesa e o voto computado
# (era 8; reduzido para 6 em 02/11. Amostra de 25/10: 0,07% dos votos abaixo de 8 s)
LIMITE_MESA_VOTO = 6

def _primeiro_horario_no_dia(caminho, data_alvo, re_dt):
    with open(caminho, 'r', encoding='utf-8', errors='ignore') as f:
        for linha in f:
            m = re_dt.search(linha)
            if m and data_alvo in m.group(1):
                return m.group(2)
    return "99:99:99"   # sem linhas no dia: vai para o fim (não contribui com nada)


def _linhas_em_ordem(arquivos, data_alvo, re_dt):
    """Linhas de todos os logs da seção (urna principal + urnas trocadas), um arquivo depois do
    outro, na ordem do primeiro horário no dia. Entre um arquivo e outro rende None."""
    ordem = sorted(arquivos, key=lambda a: _primeiro_horario_no_dia(a, data_alvo, re_dt))
    for n, caminho in enumerate(ordem):
        if n:
            yield None
        with open(caminho, 'r', encoding='utf-8', errors='ignore') as f:
            yield from f


def auditar_log_urna(caminho_log, data_alvo="04/10/2026", intervalo_minutos=15, urnas_trocadas=()):
    """
    Lê um arquivo de log da Urna Eletrônica e calcula dinamicamente
    estatísticas, anomalias e a correlação de fluxo vs. habilitação manual
    com alta granularidade temporal (padrão: blocos de 15 minutos).
    urnas_trocadas: logs de outras urnas da mesma seção (urna substituída), somados à análise.
    """
    arquivos, conteudos = [], set()
    for caminho in (caminho_log, *urnas_trocadas):
        if not os.path.exists(caminho):
            print(f"Erro: O arquivo '{caminho}' não foi encontrado.")
            sys.exit(1)
        with open(caminho, 'rb') as f:
            h = hash(f.read())
        if h not in conteudos:      # cópia idêntica de um log já incluído: ignora
            conteudos.add(h)
            arquivos.append(caminho)

    # Expressões regulares genéricas para data e eventos
    re_dt = re.compile(r'(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})')
    re_inicio = re.compile(r'(identificador.*eleitor|solicita.*t[ií]tulo|t[ií]tulo.*digitado)', re.IGNORECASE)
    re_manual = re.compile(r'(dado.*pessoal.*eleitor|habilita.*manual|mes[áa]rio.*habilitou.*eleitor|ano.*nascimento.*digitado.*igual.*cadastro)', re.IGNORECASE)
    re_fim = re.compile(r'voto.*eleitor.*computado', re.IGNORECASE)

    # Expressões para capturar a causa da habilitação manual
    re_falha_bio = re.compile(
        r'(timeout.*reconhecimento.*dedo|tentativa.*\[\d+\]|digital.*n[ãa]o.*reconhecida|timeout.*biom[eé]trico)', 
        re.IGNORECASE
    )
    re_ano_nasc  = re.compile(r'ano.*nascimento.*digitado.*igual.*cadastro', re.IGNORECASE)

    # Expressões para identificar quem autorizou a habilitação manual (digital do mesário).
    # O arquivo é lido como utf-8 ignorando erros, então os acentos (latin-1) somem: usar ".?" no lugar.
    re_pede_mes  = re.compile(r'solicita digital do mes.?rio', re.IGNORECASE)
    re_mes_cad   = re.compile(r'\tMes.?rio (\d+)')
    re_mes_rep   = re.compile(r'bate com uma digital gravada', re.IGNORECASE)
    re_mes_novo  = re.compile(r'encontrou digital coletada em nenhum dos arquivos', re.IGNORECASE)

    # Tecla indevida entre a habilitação do eleitor e o voto para Presidente (basta uma por eleitor)
    re_habilitado = re.compile(r'eleitor foi habilitado', re.IGNORECASE)
    re_tecla      = re.compile(r'tecla indevida pressionada', re.IGNORECASE)
    re_voto_pres  = re.compile(r'voto confirmado para \[presidente\]', re.IGNORECASE)

    # Última etapa de identificação feita na mesa antes do Confirma do mesário ("Eleitor foi habilitado"):
    # digital do eleitor ou do mesário capturada, dado pessoal conferido ou ano de nascimento igual ao cadastro
    re_mesa = re.compile(r'(capturada a digital|dado digitado confere|ano de nascimento digitado.*igual)', re.IGNORECASE)

    registros = []
    anomalias = []
    
    sessao_atual = {}
    seq_manual_atual = 0
    max_seq_manual = 0
    ultimo_fim_voto = None

    linhas_totais = 0
    linhas_dia = 0

    for idx_linha, linha in enumerate(_linhas_em_ordem(arquivos, data_alvo, re_dt), 1):
        if linha is None:
            # Troca de urna: o atendimento que estava aberto na urna anterior não continua na nova
            sessao_atual = {}
            continue
        linhas_totais += 1
        
        match_dh = re_dt.search(linha)
        if not match_dh:
            continue

        data_str = match_dh.group(1)
        if data_alvo not in data_str:
            continue

        linhas_dia += 1
        hora_str = match_dh.group(2)
        fmt = "%d/%m/%Y %H:%M:%S" if "/" in data_str else "%Y-%m-%d %H:%M:%S"
        dt_obj = datetime.strptime(f"{data_str} {hora_str}", fmt)

        # Quem autorizou a habilitação manual (avaliado à parte, não interfere nas causas abaixo)
        if sessao_atual:
            if re_pede_mes.search(linha):
                sessao_atual["pediu_digital_mesario"] = True
            elif sessao_atual.get("pediu_digital_mesario") and not sessao_atual.get("autorizador"):
                m_cad = re_mes_cad.search(linha)
                if m_cad:
                    sessao_atual["autorizador"] = "Mesário Cadastrado"
                    sessao_atual["titulo_mesario"] = m_cad.group(1)
                elif re_mes_rep.search(linha):
                    sessao_atual["autorizador"] = "Digital Desconhecida"
                elif re_mes_novo.search(linha):
                    # Primeira vez que essa digital autoriza: a urna a salva num arquivo novo
                    sessao_atual["autorizador"] = "Digital Desconhecida"
                    sessao_atual["digital_nova"] = True

        # Tecla indevida (avaliado à parte, não interfere nas causas abaixo)
        if sessao_atual:
            if re_mesa.search(linha) and not sessao_atual.get("habilitado"):
                sessao_atual["ultima_etapa_mesa"] = dt_obj
            if re_habilitado.search(linha):
                sessao_atual["janela_tecla"] = True
                sessao_atual.setdefault("habilitado", dt_obj)
            elif re_voto_pres.search(linha):
                sessao_atual["janela_tecla"] = False
            elif re_tecla.search(linha) and sessao_atual.get("janela_tecla"):
                sessao_atual["tecla_indevida"] = True

        # Início de Atendimento (Define padrão como Biométrica)
        if re_inicio.search(linha):
            intervalo = None
            if ultimo_fim_voto is not None:
                intervalo = (dt_obj - ultimo_fim_voto).total_seconds()

            sessao_atual = {
                "linha_inicio": idx_linha,
                "inicio": dt_obj,
                "tipo_habilitacao": "Biométrica",
                "motivo_manual": None,
                "intervalo_transicao": intervalo
            }

        # Identificação de falha/timeout biométrico antes da habilitação manual
        elif re_falha_bio.search(linha) and sessao_atual:
            sessao_atual["motivo_manual"] = "Falha/Timeout de Biometria"

        # Confirmação de Habilitação Manual pelo mesário
        elif re_manual.search(linha) and sessao_atual:
            sessao_atual["tipo_habilitacao"] = "Manual/Excepcional"
            if not sessao_atual.get("motivo_manual"):
                # Ano de nascimento sem tentativa de digital antes (senão o motivo já seria Falha/Timeout)
                if re_ano_nasc.search(linha):
                    sessao_atual["motivo_manual"] = "Sem Tentativa Biométrica (Ano de Nascimento)"
                else:
                    sessao_atual["motivo_manual"] = "Não Especificado no Log"

        # Confirmação do Voto (Fim de Ciclo)
        elif re_fim.search(linha) and sessao_atual.get("inicio"):
            sessao_atual["fim"] = dt_obj
            duracao = (dt_obj - sessao_atual["inicio"]).total_seconds()
            sessao_atual["duracao_segundos"] = duracao
            # Divide o tempo total em habilitação (título → habilitado) e cabine (habilitado → computado)
            if sessao_atual.get("habilitado"):
                sessao_atual["tempo_habilitacao"] = (sessao_atual["habilitado"] - sessao_atual["inicio"]).total_seconds()
                sessao_atual["tempo_cabine"] = (dt_obj - sessao_atual["habilitado"]).total_seconds()
            # Não depende de quando o mesário aperta Confirma nem de quando o eleitor sai da mesa
            if sessao_atual.get("ultima_etapa_mesa"):
                sessao_atual["tempo_mesa_voto"] = (dt_obj - sessao_atual["ultima_etapa_mesa"]).total_seconds()
            ultimo_fim_voto = dt_obj

            if 0 <= duracao <= 3600:
                registros.append(sessao_atual)

                # Anomalia 1: Manuais em sequência
                if sessao_atual["tipo_habilitacao"] == "Manual/Excepcional":
                    seq_manual_atual += 1
                    if seq_manual_atual > max_seq_manual:
                        max_seq_manual = seq_manual_atual
                    if seq_manual_atual >= 3:
                        anomalias.append({
                            "horario": dt_obj.strftime("%H:%M:%S"),
                            "tipo": "Sequência Manual em Lote",
                            "detalhe": f"{seq_manual_atual} eleitores consecutivos habilitados manualmente"
                        })
                else:
                    seq_manual_atual = 0

                # Anomalia 2: Votação Ultrarrápida (<10s)
                if duracao < 10:
                    tipo_hab = sessao_atual["tipo_habilitacao"]
                    anomalias.append({
                        "horario": dt_obj.strftime("%H:%M:%S"),
                        "tipo": "Votação Ultrarrápida (<10s)",
                        "detalhe": f"Tempo total: {duracao:.0f}s | Habilitação: {tipo_hab}"
                    })

                # Anomalia 2b: Mesa→Voto Ultrarrápido (última etapa na mesa → voto computado)
                if sessao_atual.get("tempo_mesa_voto") is not None and sessao_atual["tempo_mesa_voto"] < LIMITE_MESA_VOTO:
                    anomalias.append({
                        "horario": dt_obj.strftime("%H:%M:%S"),
                        "tipo": f"Mesa→Voto Ultrarráp. (<{LIMITE_MESA_VOTO}s)",
                        "detalhe": f"Mesa→voto: {sessao_atual['tempo_mesa_voto']:.0f}s | Cabine: {sessao_atual['tempo_cabine']:.0f}s | Habilitação: {sessao_atual['tipo_habilitacao']}"
                    })

                # Anomalia 3: Transição Imediata
                if sessao_atual["intervalo_transicao"] is not None and sessao_atual["intervalo_transicao"] <= 2:
                    anomalias.append({
                        "horario": dt_obj.strftime("%H:%M:%S"),
                        "tipo": "Transição Imediata (≤2s)",
                        "detalhe": f"Novo eleitor iniciado {sessao_atual['intervalo_transicao']:.0f}s após voto anterior"
                    })

            sessao_atual = {}

    total_votos = len(registros)

    print("\n" + "=" * 80)
    print("         RELATÓRIO FORENSE DE AUDITORIA (GRANULARIDADE: 15 MINUTOS)")
    print("=" * 80)
    print(f" Arquivo lido               : {os.path.basename(caminho_log)}")
    if len(arquivos) > 1:
        print(f" Urnas trocadas incluídas   : {len(arquivos) - 1} ({', '.join(os.path.basename(a) for a in arquivos[1:])})")
    print(f" Data analisada              : {data_alvo}")
    print(f" Granularidade Temporal     : A cada {intervalo_minutos} minutos")
    print(f" Linhas lidas no arquivo     : {linhas_totais} (no dia: {linhas_dia})")
    print(f" Total de eleitores validados: {total_votos}")
    print("=" * 80)

    if total_votos == 0:
        print("[AVISO] Nenhum ciclo completo de votação encontrado.")
        print("=" * 80 + "\n")
        return

    duracoes = [r["duracao_segundos"] for r in registros]
    manuais = [r for r in registros if r["tipo_habilitacao"] == "Manual/Excepcional"]

    # Contagem de causas de habilitação manual
    falhas_bio = len([r for r in manuais if r.get("motivo_manual") == "Falha/Timeout de Biometria"])
    ano_nasc = len([r for r in manuais if r.get("motivo_manual") == "Sem Tentativa Biométrica (Ano de Nascimento)"])
    nao_especificado = len(manuais) - (falhas_bio + ano_nasc)

    print("\n[1] MÉTRICAS E CADÊNCIA DE VOTAÇÃO")
    print("-" * 80)
    print(f" Tempo Médio por Eleitor    : {statistics.mean(duracoes):.1f} segundos")
    print(f" Mediana do Tempo           : {statistics.median(duracoes):.1f} segundos")
    print(f" Tempo Mínimo               : {min(duracoes):.1f} segundos")
    print(f" Tempo Máximo               : {max(duracoes):.1f} segundos")
    if total_votos > 1:
        print(f" Desvio Padrão              : {statistics.stdev(duracoes):.2f} segundos")
    # Tempo total = habilitação (título digitado → eleitor habilitado) + cabine (habilitado → voto computado)
    for rotulo, campo in (("Habilitação (título → habilitado)", "tempo_habilitacao"),
                          ("Cabine (habilitado → computado)", "tempo_cabine"),
                          ("Mesa → voto (última etapa na mesa → computado)", "tempo_mesa_voto")):
        tempos = [r[campo] for r in registros if campo in r]
        if not tempos:
            print(f" {rotulo}: sem 'Eleitor foi habilitado' no log")
            continue
        print(f" {rotulo}:")
        print(f"   Média {statistics.mean(tempos):.1f}s | Mediana {statistics.median(tempos):.1f}s | "
              f"Mín {min(tempos):.0f}s | Máx {max(tempos):.0f}s | Votos com o tempo: {len(tempos)}")

    print("\n[2] INDICADORES DE HABILITAÇÃO E CAUSA MANUAL")
    print("-" * 80)
    print(f" Total Habilitações Manuais  : {len(manuais)} ({(len(manuais)/total_votos)*100:.1f}%)")
    if len(manuais) > 0:
        print(f"   ├── Com Falha/Timeout de Biometria: {falhas_bio}")
        print(f"   ├── Sem Tentativa Biométrica (Ano de Nascimento): {ano_nasc}")
        print(f"   └── Não Especificado no Log      : {nao_especificado}")
    com_dig_mes = [r for r in manuais if r.get("pediu_digital_mesario")]
    if com_dig_mes:
        aut_cad = len([r for r in com_dig_mes if r.get("autorizador") == "Mesário Cadastrado"])
        aut_desc = len([r for r in com_dig_mes if r.get("autorizador") == "Digital Desconhecida"])
        dig_distintas = len([r for r in com_dig_mes if r.get("digital_nova")])
        aut_nd = len(com_dig_mes) - (aut_cad + aut_desc)
        titulos = {r["titulo_mesario"] for r in com_dig_mes if r.get("titulo_mesario")}
        print(f" Autorizadas por Digital do Mesário: {len(com_dig_mes)}")
        print(f"   ├── Mesário Cadastrado          : {aut_cad} ({len(titulos)} mesários distintos)")
        print(f"   ├── Digital Desconhecida        : {aut_desc} ({dig_distintas} digitais distintas)")
        print(f"   └── Não Especificado no Log      : {aut_nd}")
    teclas = len([r for r in registros if r.get("tecla_indevida")])
    print(f" Votos com Tecla Indevida   : {teclas} ({(teclas/total_votos)*100:.1f}%) [entre habilitação e voto p/ Presidente]")
    mesa_rapida = len([r for r in registros if r.get("tempo_mesa_voto") is not None and r["tempo_mesa_voto"] < LIMITE_MESA_VOTO])
    print(f" Votos Mesa→Voto < {LIMITE_MESA_VOTO}s    : {mesa_rapida} ({(mesa_rapida/total_votos)*100:.1f}%) [última etapa na mesa → voto computado]")
    print(f" Maior Sequência Manual     : {max_seq_manual} eleitores seguidos")
    print(f" Total de Alertas Gerados   : {len(anomalias)}")

    print("\n[3] ANOMALIAS ENCONTRADAS DINAMICAMENTE NO LOG")
    print("-" * 80)
    if not anomalias:
        print(" [✓] Nenhuma anomalia de tempo ou sequência detectada.")
    else:
        print(f" {'Horário':<10} | {'Tipo de Anomalia':<28} | Detalhamento")
        print("-" * 80)
        for a in anomalias:
            print(f" {a['horario']:<10} | {a['tipo']:<28} | {a['detalhe']}")

    print("\n[4] CORRELAÇÃO DE ALTA GRANULARIDADE (FLUXO VS. HABILITAÇÃO MANUAL)")
    print("-" * 80)
    print(f" {'Janela Temp.':<12} | {'Total Votos':<12} | {'Manuais (Qtd)':<15} | {'Taxa Manual (%)':<15} | Distribuição")
    print("-" * 80)
    
    # Agrupamento dinâmico em blocos de 15 minutos
    dist_total = {}
    dist_manual = {}

    for r in registros:
        dt_fim = r["fim"]
        minuto_arredondado = (dt_fim.minute // intervalo_minutos) * intervalo_minutos
        chave_tempo = f"{dt_fim.strftime('%H')}:{minuto_arredondado:02d}"
        
        dist_total[chave_tempo] = dist_total.get(chave_tempo, 0) + 1
        if r["tipo_habilitacao"] == "Manual/Excepcional":
            dist_manual[chave_tempo] = dist_manual.get(chave_tempo, 0) + 1

    for chave in sorted(dist_total.keys()):
        qtd_tot = dist_total[chave]
        qtd_man = dist_manual.get(chave, 0)
        pct_man = (qtd_man / qtd_tot) * 100 if qtd_tot > 0 else 0
        
        pct_global = (qtd_tot / total_votos) * 100
        barra = "█" * int(pct_global // 1.5)
        
        # Alerta visual para picos de alta proporção manual (≥33%) com volume de pelo menos 2 ocorridos
        alerta = " [ALERT]" if pct_man >= 33.3 and qtd_man >= 2 else ""
        print(f" {chave:<12} | {qtd_tot:<12} | {qtd_man:<15} | {pct_man:>13.1f}% | {barra}{alerta}")

    print(f" Total Habilitações Manuais  : {len(manuais)} ({(len(manuais)/total_votos)*100:.1f}%)")
    print("=" * 80 + "\n")

if __name__ == "__main__":
    # --urna-trocada <log> (repetível): log de outra urna da mesma seção, somado à análise
    args, trocadas = [], []
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--urna-trocada":
            trocadas.append(next(it))
        else:
            args.append(a)
    if not args:
        print("Uso no terminal: python forense.py <caminho_do_log> [data_DD/MM/AAAA] [minutos_janela] "
              "[--urna-trocada <log> ...]")
    else:
        data = args[1] if len(args) > 1 else "04/10/2026"
        janela = int(args[2]) if len(args) > 2 else 15
        auditar_log_urna(args[0], data_alvo=data, intervalo_minutos=janela, urnas_trocadas=trocadas)
