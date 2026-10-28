"""Coleta editais com proposta aberta no estado de SP via API pública do PNCP
e classifica em PRODUTOS (nossa empresa), OBRAS (construtoras parceiras) e SERVICOS.

Saída: data/editais.json — lido pelo index.html.
Uso: python coletor.py
"""
import json, re, time, unicodedata, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

API = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
UF = "SP"
MODALIDADES = {4: "Concorrência eletrônica", 5: "Concorrência presencial",
               6: "Pregão eletrônico", 7: "Pregão presencial", 8: "Dispensa"}
PRIORITARIAS = {"3510500": "Caraguatatuba", "3520400": "Ilhabela", "3527207": "Lorena",
                "3550704": "São Sebastião", "3539301": "Pirassununga"}
PAUSA = 1.2  # segundos entre chamadas — a API devolve 429 se for rápido demais
BRT = timezone(timedelta(hours=-3))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return s.lower()


RE_OBRA = re.compile(r"\bobras?\b|paviment|recapea|reforma|construcao|construir|drenagem|edificac"
                     r"|servicos? (comuns? )?de engenharia|terraplen|ampliacao d[aoe]|urbanizac"
                     r"|guias? e sarjet|calcament|infraestrutura|galeria pluvial|muro de arrimo")
RE_PRODUTO = re.compile(r"aquisic|fornecimento d[eo]s? (materia|produto|equipament|genero|insumo"
                        r"|mobili|kit|uniforme|medicament|pec|concreto|agua|alimento|brinquedo)"
                        r"|compra d[eo]|aquirir|adquirir")
RE_PRODUTO_LISTA = re.compile(r"registro de precos.*(produto|insumo|materia|equipament|genero|medicament)")
# serviço "forte": vence mesmo quando o texto também fala em aquisição/fornecimento
RE_SERVICO_FORTE = re.compile(r"prestacao de servic|locacao|manutenc|execucao de servic")
RE_SERVICO = re.compile(r"servic|assessoria|consultoria|exame|transporte|vigilancia|coleta|capacitac"
                        r"|treinamento|organizacao|plano diretor|projeto")


def classificar(objeto):
    # "mão de obra" aparece em contratos de serviço e não indica obra
    o = re.sub(r"\s+", " ", norm(objeto)).replace("mao de obra", "").replace("mao-de-obra", "")
    produto = bool(RE_PRODUTO.search(o) or RE_PRODUTO_LISTA.search(o))
    # obra só perde quando o objeto é claramente compra (ex.: aquisição de material para pavimentação)
    compra = re.match(r"\W*(objeto:? )?(aquisicao|registro de precos (para|visando) (a )?(futura )?(e eventual )?aquisicao)", o)
    if (RE_OBRA.search(o) or re.search(r"engenharia|execucao de canal|aduela", o)) and not compra:
        return "OBRAS"
    if produto and not RE_SERVICO_FORTE.search(o):
        return "PRODUTOS"
    if RE_SERVICO.search(o) or RE_SERVICO_FORTE.search(o):
        return "SERVICOS"
    return "PRODUTOS"  # objeto que é só lista de itens, sem verbo


def get(url, tentativas=6):
    espera = 5
    for _ in range(tentativas):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                corpo = r.read()
                return json.loads(corpo) if corpo else {"data": [], "totalPaginas": 0}
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(espera)
        espera = min(espera * 2, 60)
    raise RuntimeError(f"falhou após {tentativas} tentativas: {url}")


def coletar():
    hoje = datetime.now(BRT)
    data_final = (hoje + timedelta(days=365)).strftime("%Y%m%d")
    vistos, editais = set(), []
    for mod, mod_nome in MODALIDADES.items():
        pagina, total = 1, 1
        while pagina <= total:
            url = (f"{API}?dataFinal={data_final}&codigoModalidadeContratacao={mod}"
                   f"&uf={UF}&pagina={pagina}&tamanhoPagina=50")
            d = get(url)
            total = d.get("totalPaginas") or 0
            for x in d.get("data", []):
                ctrl = x.get("numeroControlePNCP")
                if ctrl in vistos:
                    continue
                vistos.add(ctrl)
                org, un = x.get("orgaoEntidade") or {}, x.get("unidadeOrgao") or {}
                ibge = str(un.get("codigoIbge") or "")
                cnpj, ano, seq = org.get("cnpj"), x.get("anoCompra"), x.get("sequencialCompra")
                editais.append({
                    "id": ctrl,
                    "trilha": classificar(x.get("objetoCompra")),
                    "objeto": (x.get("objetoCompra") or "").strip(),
                    "valor": x.get("valorTotalEstimado"),
                    "modalidade": mod_nome,
                    "srp": x.get("srp"),
                    "orgao": org.get("razaoSocial"),
                    "municipio": un.get("municipioNome"),
                    "ibge": ibge,
                    "prioritaria": ibge in PRIORITARIAS,
                    "abertura": x.get("dataAberturaProposta"),
                    "encerramento": x.get("dataEncerramentoProposta"),
                    "publicacao": x.get("dataPublicacaoPncp"),
                    "link_pncp": f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}",
                    "link_origem": x.get("linkSistemaOrigem"),
                })
            print(f"{mod_nome}: página {pagina}/{total} — {len(editais)} editais", flush=True)
            pagina += 1
            time.sleep(PAUSA)
    editais.sort(key=lambda e: e["encerramento"] or "9999")
    return {"atualizado": hoje.isoformat(timespec="minutes"), "uf": UF,
            "prioritarias": list(PRIORITARIAS.values()), "editais": editais}


if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)
    saida = coletar()
    with open("data/editais.json", "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, separators=(",", ":"))
    contagem = {}
    for e in saida["editais"]:
        contagem[e["trilha"]] = contagem.get(e["trilha"], 0) + 1
    print("TOTAL", len(saida["editais"]), contagem)
