"""Histórico de preços vencedores (homologados) nas cidades prioritárias, via API pública do PNCP.

Para cada compra de PRODUTOS publicada nos últimos 12 meses, baixa os itens e o resultado
de cada item: quem venceu e por quanto. É incremental: o que já foi baixado fica em
data/precos_cache.json, e cada rodada respeita um limite de chamadas e de tempo.

Saída: data/precos.json, lido pela aba "Preços vencedores" do index.html.
Uso: python precos.py
"""
import json, os, time, urllib.error
from datetime import datetime, timedelta

from coletor import BRT, PRIORITARIAS, VAZIO, classificar, get, ramos_lr, salvar_json

CONSULTA = "https://pncp.gov.br/api/consulta/v1/contratacoes/publicacao"
COMPRA = "https://pncp.gov.br/api/pncp/v1/orgaos/{cnpj}/compras/{ano}/{seq}"
MODALIDADES = (6, 7, 8)          # pregão eletrônico, pregão presencial, dispensa
DIAS_HISTORICO = 365
MESES_HISTORICO = 13
REFAZER_MESES = 2                # mês atual e anterior são reconsultados (compras novas/resultados)
SITUACOES_VALIDAS = {1}          # 1 = divulgada; 2 revogada, 3 anulada, 4 suspensa
REVER_ATE_DIAS = 120             # compra ainda sem resultado é revisitada por até 120 dias...
REVER_A_CADA_DIAS = 7            # ...no máximo uma vez por semana
ITENS_POR_PAGINA = 500
SALVAR_A_CADA = 20
MAX_CHAMADAS = int(os.environ.get("PRECOS_MAX_CHAMADAS", "1500"))
MAX_SEGUNDOS = int(os.environ.get("PRECOS_MAX_SEGUNDOS", str(40 * 60)))
PAUSA = 1.0
CACHE = "data/precos_cache.json"
LISTAS = "data/precos_listas.json"
SAIDA = "data/precos.json"


class LimiteAtingido(Exception):
    pass


class Orcamento:
    """Limita chamadas e tempo de relógio para a rodada caber no job do GitHub Actions."""

    def __init__(self, chamadas: int, segundos: int):
        self.restante = chamadas
        self.prazo = time.monotonic() + segundos

    def get(self, url: str):
        if self.restante <= 0 or time.monotonic() > self.prazo:
            raise LimiteAtingido
        self.restante -= 1
        time.sleep(PAUSA)
        return get(url)


def meses(hoje: datetime):
    """Meses-calendário do histórico, do mais recente ao mais antigo, como (chave, início, fim)."""
    ano, mes = hoje.year, hoje.month
    for _ in range(MESES_HISTORICO):
        ini = datetime(ano, mes, 1)
        prox = datetime(ano + (mes == 12), mes % 12 + 1, 1)
        fim = min(prox - timedelta(days=1), hoje.replace(tzinfo=None))
        yield f"{ano}-{mes:02d}", ini.strftime("%Y%m%d"), fim.strftime("%Y%m%d")
        ano, mes = (ano - 1, 12) if mes == 1 else (ano, mes - 1)


def compra_de(x: dict, cidade: str) -> dict | None:
    org = x.get("orgaoEntidade") or {}
    c = {"id": x.get("numeroControlePNCP"), "cidade": cidade, "cnpj": org.get("cnpj"),
         "ano": x.get("anoCompra"), "seq": x.get("sequencialCompra"), "orgao": org.get("razaoSocial"),
         "objeto": x.get("objetoCompra") or "", "data": (x.get("dataPublicacaoPncp") or "")[:10]}
    return c if all(c[k] for k in ("id", "cnpj", "ano", "seq")) else None


def listar_mes(api: Orcamento, ibge: str, cidade: str, mod: int, ini: str, fim: str) -> list[dict] | None:
    """Compras de produtos de um mês. None = falhou (não grava no cache, tenta na próxima rodada)."""
    compras, pagina, total = [], 1, 1
    while pagina <= total:
        url = (f"{CONSULTA}?dataInicial={ini}&dataFinal={fim}&codigoModalidadeContratacao={mod}"
               f"&codigoMunicipioIbge={ibge}&pagina={pagina}&tamanhoPagina=50")
        try:
            d = api.get(url)
        except (RuntimeError, urllib.error.HTTPError) as e:
            print(f"  mês pulado ({cidade} {ini[:6]} mod {mod}): {e}", flush=True)
            return None
        if d is VAZIO:
            break
        total = d.get("totalPaginas") or 0
        for x in d.get("data") or []:
            if x.get("situacaoCompraId") in SITUACOES_VALIDAS and classificar(x.get("objetoCompra")) == "PRODUTOS":
                c = compra_de(x, cidade)
                if c:
                    compras.append(c)
        pagina += 1
    return compras


def listar_compras(api: Orcamento, hoje: datetime, listas: dict) -> list[dict]:
    """Usa a lista guardada dos meses fechados; reconsulta só os REFAZER_MESES mais recentes."""
    for i, (chave_mes, ini, fim) in enumerate(meses(hoje)):
        for ibge, cidade in PRIORITARIAS.items():
            for mod in MODALIDADES:
                chave = f"{ibge}|{mod}|{chave_mes}"
                if chave in listas and i >= REFAZER_MESES:
                    continue
                achadas = listar_mes(api, ibge, cidade, mod, ini, fim)
                if achadas is not None:
                    listas[chave] = achadas
    validas = {f"{k}|{m}" for m, _, _ in meses(hoje) for k in (f"{i}|{mod}" for i in PRIORITARIAS for mod in MODALIDADES)}
    for chave in [k for k in listas if k not in validas]:
        del listas[chave]  # mês que saiu do histórico
    vistos, compras = set(), []
    for chave_mes, _, _ in meses(hoje):  # mais recentes primeiro, cidades intercaladas
        for ibge in PRIORITARIAS:
            for mod in MODALIDADES:
                for c in listas.get(f"{ibge}|{mod}|{chave_mes}", []):
                    if c["id"] not in vistos:
                        vistos.add(c["id"])
                        compras.append(c)
    return compras


def lista(resposta) -> list:
    return resposta if isinstance(resposta, list) else []


def resultado_valido(resultados: list) -> dict | None:
    # prefere o resultado não cancelado; se o campo não vier, aceita o primeiro
    for r in resultados:
        if "cancel" not in str(r.get("situacaoCompraItemResultadoNome") or "").lower():
            return r
    return None


def baixar_itens(api: Orcamento, c: dict) -> list[dict]:
    base = COMPRA.format(cnpj=c["cnpj"], ano=c["ano"], seq=c["seq"])
    itens, pagina = [], 1
    while True:
        pag = lista(api.get(f"{base}/itens?pagina={pagina}&tamanhoPagina={ITENS_POR_PAGINA}"))
        itens += pag
        if len(pag) < ITENS_POR_PAGINA:
            break
        pagina += 1
    saida = []
    for it in itens:
        if not isinstance(it, dict):
            continue
        reg = {"n": it.get("numeroItem"), "desc": (it.get("descricao") or "").strip(),
               "un": it.get("unidadeMedida"), "qtd": it.get("quantidade"),
               "est": it.get("valorUnitarioEstimado"), "venc": None}
        if it.get("temResultado") and reg["n"] is not None:
            r = resultado_valido(lista(api.get(f"{base}/itens/{reg['n']}/resultados")))
            if r:
                reg["venc"] = {"unit": r.get("valorUnitarioHomologado"), "fornecedor": r.get("nomeRazaoSocialFornecedor"),
                               "cnpj": r.get("niFornecedor"), "porte": r.get("porteFornecedorNome")}
        saida.append(reg)
    return saida


def dias_desde(data: str, hoje: datetime) -> int:
    try:
        return (hoje.date() - datetime.fromisoformat(data[:10]).date()).days
    except (TypeError, ValueError):
        return 10**6


def precisa_rever(c: dict, antigo: dict, hoje: datetime) -> bool:
    itens = antigo.get("itens") or []
    if itens and all(i["venc"] for i in itens):
        return False
    return (dias_desde(c["data"], hoje) <= REVER_ATE_DIAS
            and dias_desde(antigo.get("visto", ""), hoje) >= REVER_A_CADA_DIAS)


def carregar(caminho: str) -> dict:
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        print(f"AVISO: {caminho} ilegível ({e}); recomeçando do zero", flush=True)
        return {}


def montar_saida(cache: dict, hoje: datetime) -> dict:
    itens = []
    for c in cache.values():
        if dias_desde(c["data"], hoje) > DIAS_HISTORICO:
            continue
        for it in c["itens"]:
            v = it["venc"]
            if not v or not v["unit"]:
                continue
            itens.append({"desc": it["desc"], "un": it["un"], "qtd": it["qtd"], "est": it["est"],
                          "unit": v["unit"], "fornecedor": v["fornecedor"], "cnpj": v["cnpj"], "porte": v["porte"],
                          "cidade": c["cidade"], "orgao": c["orgao"], "data": c["data"],
                          "ramos": ramos_lr(it["desc"] + " " + c["objeto"], "PRODUTOS"),
                          "link": f"https://pncp.gov.br/app/editais/{c['cnpj']}/{c['ano']}/{c['seq']}"})
    itens.sort(key=lambda i: i["data"], reverse=True)
    return {"atualizado": hoje.isoformat(timespec="minutes"), "cidades": list(PRIORITARIAS.values()),
            "compras": sum(1 for c in cache.values() if dias_desde(c["data"], hoje) <= DIAS_HISTORICO),
            "itens": itens}


def salvar(cache: dict, listas: dict, hoje: datetime):
    salvar_json(LISTAS, listas)
    salvar_json(CACHE, cache)
    salvar_json(SAIDA, montar_saida(cache, hoje))


def main():
    hoje = datetime.now(BRT)
    cache, listas = carregar(CACHE), carregar(LISTAS)
    api = Orcamento(MAX_CHAMADAS, MAX_SEGUNDOS)
    baixadas = falhas = 0
    try:
        compras = listar_compras(api, hoje, listas)
        print(f"{len(compras)} compras de produtos nos últimos {DIAS_HISTORICO} dias", flush=True)
        # compras novas primeiro; revisões de compras já vistas só depois
        novas = [c for c in compras if c["id"] not in cache]
        rever = [c for c in compras if c["id"] in cache and precisa_rever(c, cache[c["id"]], hoje)]
        print(f"{len(novas)} novas, {len(rever)} para revisar", flush=True)
        for c in novas + rever:
            try:
                itens = baixar_itens(api, c)
            except (RuntimeError, urllib.error.HTTPError) as e:
                falhas += 1
                print(f"  falhou {c['id']}: {e}", flush=True)
                continue
            cache[c["id"]] = {**{k: c[k] for k in ("cidade", "cnpj", "ano", "seq", "orgao", "objeto", "data")},
                              "itens": itens, "visto": hoje.strftime("%Y-%m-%d")}
            baixadas += 1
            if baixadas % SALVAR_A_CADA == 0:
                salvar(cache, listas, hoje)
                print(f"  {baixadas} compras baixadas, {api.restante} chamadas restantes", flush=True)
    except LimiteAtingido:
        print("limite de chamadas/tempo atingido; o restante fica para a próxima rodada", flush=True)
    finally:
        os.makedirs("data", exist_ok=True)
        salvar(cache, listas, hoje)
    n = len(montar_saida(cache, hoje)["itens"])
    print(f"TOTAL: {baixadas} compras baixadas, {falhas} falhas, {len(cache)} no cache, {n} itens com preço vencedor")


if __name__ == "__main__":
    main()
