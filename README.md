# Emissor NFS-e (padrão nacional) — ISS Net

Sistema web local, em Python, para emitir **NFS-e no padrão nacional** pelo web service do **ISS Net** a partir de uma
**remessa de vendas exportada do seu ERP**. Nasceu para um volume de milhares de notas por dia no DF e foi
generalizado para que outras empresas montem o próprio emissor a partir dele.

> **Aviso:** ponto de partida, sem garantia. Você é responsável pelas notas que emitir e pelos tributos. Teste em
> homologação e valide as regras com a sua contabilidade antes de usar em produção. Leia o [SECURITY.md](SECURITY.md).

## O que ele faz

1. **Importa** o arquivo de remessa do ERP e valida cada venda (CPF/CNPJ, endereço, CEP, valores).
2. **Confere o CEP** de cada cliente (ViaCEP/BrasilAPI/IBGE) e o histórico de CEPs que o ISS Net já aceitou ou recusou.
3. Monta **lotes de produção**, reserva os números de DPS livres no ISS Net e **assina cada DPS** com o certificado A1.
4. **Emite** com proteção contra nota duplicada (consulta a DPS antes de reenviar), tratamento de falhas do ISS Net e de
   erros comuns (CEP, retenção de ISS, DPS já existente, cliente sem inscrição municipal).
5. **Confere** cada nota autorizada com o pedido (cliente, valor, boleto) e gera o **arquivo de retorno** para o ERP.
6. Painel com situação de cada remessa, filtro de datas, relatório de pedidos "sem nota", usuários, login e auditoria.

## Requisitos

- Python 3.11 ou superior
- Certificado digital **A1** (`.pfx`) da empresa emissora, para a produção e para os testes no ISS Net
- Cadastro da empresa e série de DPS autorizada no ISS Net do seu município

## Instalação rápida

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config_local.example.py config_local.py
rem edite config_local.py com os dados da sua empresa
set EMISSOR_IGNORAR_CONFIG_LOCAL=
.venv\Scripts\python app.py
```

Abra http://127.0.0.1:5000. No primeiro acesso, crie o administrador (só funciona no próprio computador). Depois:
carregue o certificado A1 pela tela (fica só na memória), importe uma remessa e siga os passos da tela.
No Windows há também `iniciar_emissor.bat`.

Em Linux/macOS use `.venv/bin/pip` e `.venv/bin/python`.

## O que você precisa adaptar

O sistema foi escrito para um caso real; estes pontos são específicos e você deve revisá-los:

| Ponto | Onde | O que mudar |
|---|---|---|
| Dados da empresa, série, códigos de serviço, alíquotas e texto da nota | `config.py` / `config_local.py` | Tudo é exemplo. Ajuste com a contabilidade |
| Layout da remessa do seu ERP | `nfse/remessa.py` | Hoje: linhas `C` (cliente), `V` (venda), `I` (item) separadas por `;` |
| Número do boleto e tipo de pacote no texto do item | `RE_BOLETO`, `RE_PACOTE` em `nfse/remessa.py` | Expressões regulares do seu texto |
| Layout do arquivo de retorno para o ERP | `nfse/retorno.py` | Colunas e nome do arquivo |
| Município e endereços do web service | `nfse/issnet.py` | Outros municípios usam outros endereços/versões |
| Regras tributárias da DPS | `nfse/dps.py` | Revise tributos, retenções e IBS/CBS |
| Nomes nas telas | `templates/` | "Gerência" é o nome do ERP do caso original; troque pelo do seu |

## Testes

```bat
set EMISSOR_IGNORAR_CONFIG_LOCAL=1
.venv\Scripts\python -m unittest discover -s tests -v
```

Os testes usam apenas dados **fictícios** (`tests/fixtures`) e não falam com a internet nem com o ISS Net.

## Estrutura

```
app.py            telas e rotas (Flask)
autenticacao.py   usuários, login, CSRF, auditoria
segredos.py       segredos aleatórios fora do código
config.py         configuração (exemplos) + config_local.py (seus dados, fora do Git)
nfse/             DPS, assinatura, ISS Net, remessa, CEP, retorno, conferência
templates/        páginas HTML
schemas/          XSD oficiais do padrão nacional
tests/            testes automatizados e fixtures fictícias
```

## Licença

MIT (ver [LICENSE](LICENSE)). Sem garantia.
