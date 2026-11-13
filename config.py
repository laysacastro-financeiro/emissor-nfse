"""Configurações do emissor de NFS-e.

Os valores abaixo são EXEMPLOS. Para o seu uso, não edite este arquivo: copie `config_local.example.py` para
`config_local.py` (que o Git ignora) e coloque lá os dados da sua empresa. Tudo que o `config_local.py` definir
substitui o que está aqui.

Nunca coloque senha, chave ou certificado neste arquivo nem no `config_local.py`: o certificado A1 é carregado
pela tela e fica só na memória; segredos do sistema vêm de variáveis de ambiente (ver segredos.py).
"""
import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DADOS_DIR = BASE_DIR / "dados"  # lotes, usuários, auditoria: nunca vai para o Git

# Ambiente de cada lote. Lote importado pela tela é sempre de homologação (sem valor fiscal).
# Produção só existe em lote marcado com ambiente.txt = "producao" (criado à parte, nunca pela tela).
AMBIENTE = "homologacao"
# TRAVA DE SEGURANÇA: com False o sistema recusa qualquer envio em produção (notas com valor fiscal).
# Só mude para True (no config_local.py) depois de validar tudo em homologação e com a contabilidade.
PERMITIR_PRODUCAO = False
LIMITE_NOTAS_PRODUCAO_POR_LOTE = 2500  # trava: nenhum lote de produção maior que isso
LIMITE_AMOSTRA_PRODUCAO = 5  # lote de teste ("uma de cada pacote")
ENVIO_POR_CLIQUE_PRODUCAO = 400  # o envio roda dentro da requisição: blocos de 400
# Ao importar uma remessa, o sistema já confere os CEPs de todas as vendas em segundo plano.
VERIFICAR_CEPS_AO_IMPORTAR = True
# Se o endereço do CNPJ na Receita Federal tem CEP válido, do mesmo município e com a mesma rua, usar esse CEP sem a
# equipe confirmar (fica na auditoria). False = só sugere.
APLICAR_CEP_DA_RECEITA_AUTOMATICAMENTE = False
# Pasta onde o arquivo de retorno (para o seu ERP) é salvo, além do download.
PASTA_RETORNO_GERENCIA = BASE_DIR / "retorno_gerencia"
VER_APLIC = "EmissorNFSe-0.1"

# Dados do PRESTADOR (a sua empresa). Exemplo fictício: troque no config_local.py.
PRESTADOR = {
    "cnpj": "11222333000181",
    "im": "0000000000001",  # inscrição municipal
    "fone": "1100000000",
    "email": "contato@example.com",
    "op_simp_nac": "1",  # 1 = não optante do Simples Nacional
    "reg_esp_trib": "0",  # 0 = nenhum regime especial
}
COD_MUNICIPIO_EMISSOR = "5300108"  # código IBGE do município emissor (exemplo: Brasília/DF)

# Série da DPS. A faixa 00001–49999 é a de aplicativo próprio (web service). No ISS Net DF a série precisa ser a
# autorizada para a empresa; o sistema reserva os números livres dela pelo botão "Usar série autorizada".
SERIE_DPS = "1"

SERVICO = {
    "c_trib_nac": "010501",  # código de tributação nacional (exemplo: licenciamento de software)
    # Código municipal da atividade, como está no cadastro da empresa no município.
    "c_trib_mun": "105",
    "c_nbs": "111032200",
    "aliquota_iss": Decimal("2.00"),
    # Não optante do Simples com ISS devido no próprio município: a DPS não pode trazer pAliq (E0617).
    "informar_aliquota": False,
    "trib_issqn": "1",  # operação tributável
    "tp_ret_issqn": "1",  # 1 = não retido
    "cst_piscofins": "00",
    "tp_ret_piscofins": "0",
}

# A DPS leva só a classificação; os valores de IBS/CBS são calculados pelo ISS Net.
IBSCBS = {
    "fin_nfse": "0",
    "c_ind_op": "100501",
    "ind_dest": "0",
    "cst": "000",
    "c_class_trib": "000001",
}

# Tributos sobre o faturamento informados na composição do valor (informações complementares). EXEMPLO: confirme as
# alíquotas e o texto com a sua contabilidade antes de usar.
TRIBUTOS_FATURAMENTO = (("PIS", Decimal("0.65")), ("COFINS", Decimal("3")), ("ISS", Decimal("2")))
TEXTO_INF_COMPL = (  # EXEMPLO de empresa de software: troque pelo texto da sua atividade
    "Conforme SC Cosit n° 157/2023 e 243/2017, ficam dispensadas as retenções na fonte do "
    "licenciamento ou cessão de uso de software sem que o contrato estabeleça a prestação do "
    "serviço de programação."
)

# Arquivo de retorno para o ERP: NOME = {CODIGO_EMPRESA_RETORNO}nota_DDMMAAAAHHMMSS.txt
CODIGO_EMPRESA_RETORNO = "00000"
# Endereço público onde o XML e o PDF das notas ficam disponíveis. Enquanto tiver "A-DEFINIR" e o modo for
# "hospedagem", o retorno é bloqueado (para não mandar links inválidos aos clientes).
URL_BASE_ARQUIVOS = "https://A-DEFINIR/nfse"
# "hospedagem": links para URL_BASE_ARQUIVOS. "consulta_publica": links para a consulta pública nacional da nota.
MODO_URLS_RETORNO = "consulta_publica"
# Tamanho máximo do campo de chave do seu ERP (o nacional tem 50 dígitos).
TAMANHO_MAX_CHAVE_GERENCIA = 44

# Rede: "127.0.0.1" = só neste computador (recomendado). Para a equipe acessar pela rede, use 0.0.0.0 (ou a variável
# de ambiente EMISSOR_HOST) e coloque o sistema atrás de HTTPS (ver SECURITY.md).
HOST = os.environ.get("EMISSOR_HOST", "127.0.0.1")
PORTA = int(os.environ.get("EMISSOR_PORTA", "5000"))

# Segredos (cookie de sessão e links dos arquivos): não ficam aqui. Vêm das variáveis de ambiente
# EMISSOR_SEGREDO_SESSAO / EMISSOR_SEGREDO_URL ou de um valor aleatório gerado em dados/ (ver segredos.py).
# Marque como True se o sistema for servido por HTTPS: o cookie de sessão só trafega criptografado.
COOKIE_SEGURO = os.environ.get("EMISSOR_COOKIE_SEGURO", "") == "1"

# Ajustes do seu ambiente (fora do Git). Os testes automáticos ignoram este arquivo
# (variável de ambiente EMISSOR_IGNORAR_CONFIG_LOCAL=1), para rodarem sempre com os dados de exemplo.
if not os.environ.get("EMISSOR_IGNORAR_CONFIG_LOCAL"):
    try:
        from config_local import *  # noqa: F401,F403
    except ImportError:
        pass
