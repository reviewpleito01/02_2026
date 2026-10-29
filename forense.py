import re
import sys
import os
import statistics
from datetime import datetime

def auditar_log_urna(caminho_log, data_alvo="04/10/2026", intervalo_minutos=15):
    """
    Lê um arquivo de log da Urna Eletrônica e calcula dinamicamente 
    estatísticas, anomalias e a correlação de fluxo vs. habilitação manual 
    com alta granularidade temporal (padrão: blocos de 15 minutos).
    """
    if not os.path.exists(caminho_log):
        print(f"Erro: O arquivo '{caminho_log}' não foi encontrado.")
        sys.exit(1)

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
    re_sem_bio   = re.compile(r'(sem.*biometria|n[ãa]o.*possui.*biometria|n[ãa]o.*cadastrad)', re.IGNORECASE)
    re_ano_nasc  = re.compile(r'ano.*nascimento.*digitado.*igual.*cadastro', re.IGNORECASE)

    registros = []
    anomalias = []
    
    sessao_atual = {}
    seq_manual_atual = 0
    max_seq_manual = 0
    ultimo_fim_voto = None

    linhas_totais = 0
    linhas_dia = 0

    with open(caminho_log, 'r', encoding='utf-8', errors='ignore') as f:
        for idx_linha, linha in enumerate(f, 1):
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

            # Identificação de ausência de cadastro biométrico
            elif re_sem_bio.search(linha) and sessao_atual:
                sessao_atual["motivo_manual"] = "Sem Biometria Cadastrada"

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

                    # Anomalia 2: Votação Ultrarrápida (<15s)
                    if duracao < 15:
                        tipo_hab = sessao_atual["tipo_habilitacao"]
                        anomalias.append({
                            "horario": dt_obj.strftime("%H:%M:%S"),
                            "tipo": "Votação Ultrarrápida (<15s)",
                            "detalhe": f"Tempo total: {duracao:.0f}s | Habilitação: {tipo_hab}"
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
    sem_bio = len([r for r in manuais if r.get("motivo_manual") == "Sem Biometria Cadastrada"])
    ano_nasc = len([r for r in manuais if r.get("motivo_manual") == "Sem Tentativa Biométrica (Ano de Nascimento)"])
    nao_especificado = len(manuais) - (falhas_bio + sem_bio + ano_nasc)

    print("\n[1] MÉTRICAS E CADÊNCIA DE VOTAÇÃO")
    print("-" * 80)
    print(f" Tempo Médio por Eleitor    : {statistics.mean(duracoes):.1f} segundos")
    print(f" Mediana do Tempo           : {statistics.median(duracoes):.1f} segundos")
    print(f" Tempo Mínimo               : {min(duracoes):.1f} segundos")
    print(f" Tempo Máximo               : {max(duracoes):.1f} segundos")
    if total_votos > 1:
        print(f" Desvio Padrão              : {statistics.stdev(duracoes):.2f} segundos")

    print("\n[2] INDICADORES DE HABILITAÇÃO E CAUSA MANUAL")
    print("-" * 80)
    print(f" Total Habilitações Manuais  : {len(manuais)} ({(len(manuais)/total_votos)*100:.1f}%)")
    if len(manuais) > 0:
        print(f"   ├── Com Falha/Timeout de Biometria: {falhas_bio}")
        print(f"   ├── Sem Biometria Cadastrada     : {sem_bio}")
        print(f"   ├── Sem Tentativa Biométrica (Ano de Nascimento): {ano_nasc}")
        print(f"   └── Não Especificado no Log      : {nao_especificado}")
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
    if len(sys.argv) < 2:
        print("Uso no terminal: python script_de_auditoria.py <caminho_do_log> [data_DD/MM/AAAA] [minutos_janela]")
    else:
        data = sys.argv[2] if len(sys.argv) > 2 else "04/10/2026"
        janela = int(sys.argv[3]) if len(sys.argv) > 3 else 15
        auditar_log_urna(sys.argv[1], data_alvo=data, intervalo_minutos=janela)
