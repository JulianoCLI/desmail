- [x] Lifecycle/create/status e testes offline.
- [x] Guard/deadline/navegacao leve com JS e testes.
- [x] Cliente/docs/launcher consistentes.
- [x] Suite inteira e Chrome local; registrar resultados e limites.

Verificacao final:
- `python -m pytest -q -p no:cacheprovider --tb=short`: 116 passed in 3.98s.
- `python local_browser_check.py`: PASS: localhost Chrome; isolated tab closed; inbox preserved; opened not verified; JS leve permitido; production localhost rejected.
- Falha inicial TestClient: Starlette exige httpx2 ausente; harness ASGI offline
  substituiu TestClient sem dependencia adicional.
- Falha inicial Chrome: favicon da pagina inbox contaminava contador; inbox data:
  isola teste sem relaxar politica de producao.
- Limites DNS/egress e perfil compartilhado no README.
