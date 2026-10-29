# Roteiro para uso dos scripts


## No script Download configure com os dados da Zona:

UF = "sp"  
MUNICIPIO = "71072"  
ZONA = "0247" # Número da zona a ser baixada  
DESTINO = Path("logs_saopaulo_247")  # Caminho para salvar os arquivos  



### Código do município 'mu' (ver link gerado no portal de Resultados ao baixar um Log de urna)  

* Exemplo: https://resultados.tse.jus.br/oficial/app/index.html#/eleicao/6258/uf/sp/mu/71072/zn/0001/cargo/1/vis/nominal/se/0001/dados-de-urna/boletim-de-urna

* No caso acima 'mu/71072'  


### Intervalo de Seções

Linha 94 do script - preencher com o intervalo de seções na zona.

secoes = range(1, 972) 


## Execução

python dowload.py

Em seguida extraia todos os zips para uma pasta

## Script Forense

### No script forense, execute com a data 25/10/2026:

python forense.py ~/logs_salvador/0363-686d5775-o03221ba3849000050363-log/logd.dat 25/10/2026



