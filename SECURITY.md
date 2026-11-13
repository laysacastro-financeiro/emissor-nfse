# Segurança

Este sistema emite notas fiscais e guarda dados de clientes. Leia antes de usar.

## O que nunca deve ir para o Git

Já está no `.gitignore`, mas confira antes de cada `git push`:

- a pasta `dados/` (lotes, notas, clientes, usuários com hash de senha, segredo de sessão, auditoria);
- `config_local.py` (dados da sua empresa);
- certificados (`*.pfx`, `*.p12`, `*.pem`, `*.key`) e qualquer senha, token ou chave de API;
- remessas, notas e retornos reais. Os arquivos de `tests/fixtures/` são **fictícios** e assim devem continuar.

Se um segredo for publicado por engano: troque-o imediatamente (apagar o commit não basta; o histórico fica visível).
Para o certificado A1, peça a revogação/reemissão à autoridade certificadora.

## Como o sistema se protege

- **Certificado A1:** carregado pela tela, fica só na memória do processo; nada é gravado em disco. Fechar o programa o descarta.
- **Senhas dos usuários:** guardadas só como hash (PBKDF2/scrypt do Werkzeug). Mínimo de 8 e máximo de 128 caracteres.
- **Login:** bloqueio de 15 minutos após 5 senhas erradas (por login) ou 20 (por endereço); mesmo tempo de resposta para login existente ou não.
- **Sessão:** cookie `HttpOnly` e `SameSite=Lax`; expira em 12 horas; use `EMISSOR_COOKIE_SEGURO=1` quando houver HTTPS.
- **CSRF:** todo `POST` exige token da sessão.
- **Primeiro acesso:** só cria o administrador a partir do próprio computador (defina `EMISSOR_PRIMEIRO_ACESSO_REMOTO=1` só se souber o que está fazendo).
- **Segredos do sistema:** nunca no código. Vêm de variáveis de ambiente (`EMISSOR_SEGREDO_SESSAO`, `EMISSOR_SEGREDO_URL`) ou de um valor aleatório criado em `dados/`.
- **XML:** respostas do ISS Net são lidas sem entidades externas nem rede; notas enviadas pela tela com `DOCTYPE`/`ENTITY` são recusadas.
- **Cabeçalhos:** `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- **Produção fiscal:** `PERMITIR_PRODUCAO = False` por padrão. Sem mudar isso, o sistema recusa emitir notas reais.

## Colocar na rede

O servidor embutido é HTTP simples. Para uso pela equipe:

1. mantenha `HOST=127.0.0.1` e coloque um proxy reverso com HTTPS (nginx, Caddy, IIS) na frente; ou use VPN;
2. defina `EMISSOR_COOKIE_SEGURO=1`;
3. **crie o administrador antes de expor o sistema** (o primeiro acesso só funciona localmente);
4. libere a porta só para a rede da empresa; nunca exponha o sistema na internet aberta;
5. faça backup da pasta `dados/` (ela guarda o controle de numeração das DPS: perdê-la pode gerar números repetidos).

Observe que, atrás de um proxy reverso no mesmo computador, todos os acessos aparecem como `127.0.0.1`.

## Avisos

- O projeto é um ponto de partida, **sem garantia**. A responsabilidade pela emissão das notas e pelos tributos é da sua empresa e da sua contabilidade.
- Teste sempre em homologação antes de qualquer envio em produção.
- Os serviços de CEP e CNPJ (ViaCEP, BrasilAPI, minhareceita.org, IBGE) são públicos e externos: o sistema envia a eles CEPs e CNPJs de clientes. Se isso não for aceitável para você, desative `VERIFICAR_CEPS_AO_IMPORTAR`.

## Como reportar uma vulnerabilidade

Abra uma *security advisory* privada no GitHub (aba **Security** do repositório) em vez de uma issue pública.
