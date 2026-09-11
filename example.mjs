// Executar explicitamente: node example.mjs. Cria caixa no provedor real.
import {createEmail, getStatus, releaseEmail} from './client.js';

const {email, session_id} = await createEmail({
  auto_confirm: true, expected_hosts: ['accounts.example.org'], timeout_seconds: 600,
});
console.log({email, session_id});
// Sua integracao envia cadastro para email. Troque expected_hosts pelo alvo real.
for (;;) {
  const status = await getStatus(session_id);
  console.log(status.state, status.error);
  if (!status.monitoring) break; // opened nao comprova verificacao da conta.
  await new Promise(resolve => setTimeout(resolve, 15000));
}
// Ciclo criar > usar > confirmar > apagar: release libera a memoria na hora
// e limpa a caixa no provedor em segundo plano. O proximo create nao espera.
console.log(await releaseEmail(session_id));
