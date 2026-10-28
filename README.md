# Licitações SP — Urban

Painel de editais com proposta aberta no estado de São Paulo, separado do painel da Flórida.

- **Fonte:** API pública do PNCP (Portal Nacional de Contratações Públicas). Não precisa de login nem de chave.
- **Trilhas:**
  - **Produtos:** fornecimento de material, para a nossa empresa.
  - **Obras:** obras e engenharia, para as construtoras parceiras. Cada edital tem um botão de WhatsApp.
  - **Serviços:** só para consulta.
- **Cidades prioritárias:** Caraguatatuba, Ilhabela, Lorena, São Sebastião e Pirassununga.
- **Atualização:** o GitHub Actions roda `coletor.py` todo dia às 7h de Brasília e grava `data/editais.json`.

Para rodar localmente: `python coletor.py` e depois abrir `index.html` com um servidor estático (`python -m http.server`).
