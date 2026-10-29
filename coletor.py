"""Coleta editais com proposta aberta no estado de SP via API pública do PNCP
e classifica em PRODUTOS (nossa empresa), OBRAS (construtoras parceiras) e SERVICOS.

Saída: data/editais.json — lido pelo index.html.
Uso: python coletor.py
"""
import http.client, json, os, re, time, unicodedata, urllib.request, urllib.error
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
                     r"|guias? e sarjet|calcament|infraestrutura|galerias? (de aguas )?pluvia|muro de arrimo")
RE_PRODUTO = re.compile(r"aquisic|fornecimento d[eo]s? (materia|produto|equipament|genero|insumo"
                        r"|mobili|kit|uniforme|medicament|pec|concreto|agua|alimento|brinquedo)"
                        r"|compra d[eo]|aquirir|adquirir")
RE_PRODUTO_LISTA = re.compile(r"registro de precos.*(produto|insumo|materia|equipament|genero|medicament)")
# serviço "forte": vence mesmo quando o texto também fala em aquisição/fornecimento
RE_SERVICO_FORTE = re.compile(r"prestacao de servic|locacao|manutenc|execucao de servic")
RE_SERVICO = re.compile(r"servic|assessoria|consultoria|exame|transporte|vigilancia|coleta|capacitac"
                        r"|treinamento|organizacao|plano diretor|projeto")


# Ramos que a L R SOLUCOES LTDA (CNPJ 24.615.728/0001-96) pode disputar, agrupando seus CNAEs.
# Edital que casa com algum ramo recebe a marca "L R pode disputar" no painel.
RAMOS_LR = {
    "Esporte e lazer": r"tatame|\beva\b|esportiv|colchonete|\bbolas?\b|brinquedo|playground|recreativ|medalha|trofeu|camping",
    "Papelaria e escolar": r"papelaria|material (de )?escritorio|material escolar|kits? escolar|caderno|\blivros?\b|sulfite|expediente|^\W*papel\W*$|papel a4|resma",
    "Informática e eletrônicos": r"informatica|computador|notebook|impressora|toner|cartucho|monitor|tablet|eletronic|telefon|projetor|\bcamera|nobreak|scanner|lousa digital",
    "Eletrodomésticos e móveis": r"eletrodomestic|refrigerador|geladeira|freezer|fogao|ar[- ]condicionado|ventilador|bebedouro|micro-?ondas|mobiliari|\bmoveis\b|cadeiras?\b|armario|colch(ao|oes)",
    "Limpeza e higiene": r"limpeza|higiene|saneante|detergente|desinfetante|sabonete|papel higienico|papel toalha|sacos? (de|para) lixo|descartave|copos? descartav",
    "Alimentos e bebidas": r"genero alimenticio|generos alimenticios|alimentos?\b|cesta basica|\bcafe\b|acucar|\bleite|merenda|agua mineral|bebidas?|hortifruti|\bcarnes?\b|lanches?",
    "Médico-hospitalar": r"medicament|hospitalar|material medico|odontolog|enfermagem|laboratori|curativo|seringa|agulha|luvas? (de )?procedimento|insumos? (de|para) saude|bandagem|equipamentos? (medic|odonto|hospitalar)|materia(l|is) de consumo (medic|hospitalar)",
    "Vestuário, EPI e têxtil": r"uniforme|camiseta|vestuario|calcado|botas?\b|botina|\bepis?\b|equipamentos? de protecao|colete|cama,? mesa e banho|toalha|lencol|lencois|tecido|enxoval|agasalho",
    "Construção, ferragens e elétrico": r"material de construcao|materiais de construcao|ferragen|ferramenta|material eletrico|materiais eletricos|lampada|cimento|\btintas?\b|madeira|hidraulic|\bareia\b|\bbrita\b|concreto|luminaria|refletor|maquinas? pesada|linha amarela|retroescavadeira|\btrator",
    "Peças automotivas": r"pecas? (automotiv|para (manutencao (de|dos|da frota de )?)?veicul|e acessorios para veicul)|autopeca|\bpneus?\b|bateria automotiv|lubrificante",
    "Eventos, palco e som": r"\bpalco|\btendas?\b|sonorizacao|estrutura (para|de) eventos|iluminacao cenica|festividade|carnaval|reveillon",
    "Veículos": r"veicul|motocicleta|micro-?onibus|onibus|pick-?up|caminhonete|van|automovel|ambulancia|carretinha|reboque",
    "Mercadorias em geral": r"sacolas?\b|sacos? plastic|material de consumo|materiais de consumo|utensilios|copa e cozinha|descartaveis",
    "Ração animal": r"\bracao|racoes|alimentos? para (caes|gatos|animais|equinos)",
}
RAMOS_LR = {k: re.compile(v) for k, v in RAMOS_LR.items()}
RAMO_OBRAS = "Obras civis (precisa de responsável técnico CREA)"


def ramos_lr(objeto, trilha):
    o = norm(objeto)
    ramos = [nome for nome, rx in RAMOS_LR.items() if rx.search(o)] if trilha == "PRODUTOS" else []
    if trilha == "OBRAS":
        ramos.append(RAMO_OBRAS)
    return ramos


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


VAZIO = object()  # resposta 204/corpo vazio: a API usa isso para "nenhum registro"


def get(url, tentativas=6, vazio=VAZIO):
    """GET com retry. Corpo vazio devolve `vazio` (padrão: o marcador VAZIO)."""
    espera = 5
    for _ in range(tentativas):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "urban-licitacoes-br/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                corpo = r.read()
            return json.loads(corpo) if corpo.strip() else vazio
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, OSError, http.client.HTTPException, json.JSONDecodeError):
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
            if d is VAZIO:
                if pagina > 1:  # vazio no meio da paginação = falha da API, não fim dos dados
                    raise RuntimeError(f"página vazia inesperada: {url}")
                break
            total = d.get("totalPaginas") or 0
            for x in d.get("data") or []:
                ctrl = x.get("numeroControlePNCP")
                if not ctrl or ctrl in vistos:
                    continue
                vistos.add(ctrl)
                org, un = x.get("orgaoEntidade") or {}, x.get("unidadeOrgao") or {}
                ibge = str(un.get("codigoIbge") or "")
                cnpj, ano, seq = org.get("cnpj"), x.get("anoCompra"), x.get("sequencialCompra")
                trilha = classificar(x.get("objetoCompra"))
                editais.append({
                    "id": ctrl,
                    "trilha": trilha,
                    "ramos": ramos_lr(x.get("objetoCompra"), trilha),
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


def salvar_json(caminho, dados):
    """Grava em arquivo temporário e troca no fim: um job interrompido não deixa JSON truncado."""
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, caminho)


SAIDA = "data/editais.json"
QUEDA_MAXIMA = 0.5  # recusa sobrescrever se a coleta vier com menos da metade dos editais anteriores

if __name__ == "__main__":
    os.makedirs("data", exist_ok=True)
    saida = coletar()
    try:
        with open(SAIDA, encoding="utf-8") as f:
            anterior = len(json.load(f)["editais"])
    except (OSError, ValueError, KeyError):
        anterior = 0
    if len(saida["editais"]) < anterior * QUEDA_MAXIMA:
        raise SystemExit(f"coleta suspeita: {len(saida['editais'])} editais contra {anterior} antes; mantendo o arquivo anterior")
    salvar_json(SAIDA, saida)
    contagem = {}
    for e in saida["editais"]:
        contagem[e["trilha"]] = contagem.get(e["trilha"], 0) + 1
    print("TOTAL", len(saida["editais"]), contagem)
