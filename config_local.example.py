"""Seus dados. Copie este arquivo para `config_local.py` (o Git ignora esse nome) e edite.

Só coloque aqui o que muda em relação ao config.py. NUNCA coloque senha, chave ou certificado.
"""
from decimal import Decimal  # noqa: F401  (use se precisar de valores decimais)

PRESTADOR = {
    "cnpj": "00000000000000",  # CNPJ da sua empresa (só números)
    "im": "0000000000000",  # inscrição municipal
    "fone": "0000000000",
    "email": "fiscal@suaempresa.com.br",
    "op_simp_nac": "1",  # 1 = não optante do Simples Nacional
    "reg_esp_trib": "0",
}
COD_MUNICIPIO_EMISSOR = "0000000"  # código IBGE do seu município
SERIE_DPS = "1"
CODIGO_EMPRESA_RETORNO = "00000"

# Só depois de validar tudo em homologação e combinar com a contabilidade:
# PERMITIR_PRODUCAO = True
