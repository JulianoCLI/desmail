# Plano: API autonoma

1. Lifecycle/status: worker unico, tick 15s, create validado, cache consultavel.
   Aceite: sem TUI, sem Chrome no startup vazio, shutdown aguarda operacao.
2. Navegacao: reutilizar parser/cache/dedup; guard compartilhado; alvo exato,
   DNS publico, navegacao leve com JS na aba criada, somente aba criada.
   Aceite: mismatch/private bloqueados; timeout fecha aba, sem retry.
3. Integracao: helper JS, exemplo, README e launcher single process.
   Aceite: suite completa e teste Chrome localhost, sem tokens/criacao externos.

Dependencias: 2 usa estado de 1; 3 verifica 1 e 2.
Verificacao: `python -m pytest -q -p no:cacheprovider`,
`python local_browser_check.py`.
Risco: DNS preflight nao fixa resolucao Chrome; documentar TOCTOU e necessidade
de filtro de egress para garantia de rede.
