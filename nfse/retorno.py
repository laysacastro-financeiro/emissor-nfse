"""Arquivo de retorno para o Gerência — mesmo layout gerado pelo emissor anterior.

  NR_PEDIDO;NR_NOTA;NR_MENSAGEM;TX_CHAVE;URL_XML;URL_PDF
UTF-8 sem BOM, quebra de linha LF, uma linha por nota autorizada (status 100).
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime

import config
import segredos

from .nfse_xml import NotaAutorizada

CAMPOS_RETORNO = ("NR_PEDIDO", "NR_NOTA", "NR_MENSAGEM", "TX_CHAVE", "URL_XML", "URL_PDF")
URL_CONSULTA_PUBLICA = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave={chave}"


class RetornoNaoConfigurado(Exception):
    """Faltou definir onde ficam os arquivos: um retorno com links falsos chegaria aos clientes."""


def token_arquivo(chave: str) -> str:
    """Parte imprevisível da URL, para que ninguém descubra as notas de outros clientes trocando números."""
    segredo = segredos.segredo("segredo_url", "EMISSOR_SEGREDO_URL")  # aleatório e fora do código
    return hmac.new(segredo.encode(), chave.encode(), hashlib.sha256).hexdigest()[:20]


def urls_da_nota(chave: str) -> tuple[str, str]:
    if config.MODO_URLS_RETORNO == "consulta_publica":
        url = URL_CONSULTA_PUBLICA.format(chave=chave)
        return url, url
    if "A-DEFINIR" in config.URL_BASE_ARQUIVOS:
        raise RetornoNaoConfigurado(
            "O endereço público dos XML/PDF ainda não foi definido (config.URL_BASE_ARQUIVOS). "
            "O retorno não foi gerado para não mandar links inválidos aos clientes."
        )
    base = f"{config.URL_BASE_ARQUIVOS.rstrip('/')}/{token_arquivo(chave)}"
    return f"{base}/{chave}-nfse.xml", f"{base}/{chave}.pdf"


def chave_para_gerencia(pedido: str, nota: NotaAutorizada) -> str:
    """TX_CHAVE no formato curto do emissor anterior: CNPJ_pedido_nDPS_série(3)_nNFSe (~37 caracteres).

    A chave nacional tem 50 dígitos e não cabe no campo CHAVE_NFE do Gerência (documentado com 44):
    a importação de 21/10/2026 com a chave de 50 deu erro 500. A chave completa segue no XML da nota."""
    chave = (f"{config.PRESTADOR['cnpj']}_{pedido}_{int(nota.numero_dps or 0)}_"
             f"{int(nota.serie_dps or 0):03d}_{nota.numero}")
    if len(chave) > config.TAMANHO_MAX_CHAVE_GERENCIA:
        raise RetornoNaoConfigurado(f"Chave para o Gerência com {len(chave)} caracteres: {chave}")
    return chave


def gerar_retorno(pares: list[tuple[str, NotaAutorizada]], gerado_em: datetime) -> tuple[str, bytes]:
    """Recebe (pedido, nota) já conferidos e devolve (nome do arquivo, conteúdo)."""
    linhas = []
    for pedido, nota in pares:
        if nota.status != "100" or not nota.chave:
            continue  # o Gerência só aceita notas autorizadas
        url_xml, url_pdf = urls_da_nota(nota.chave)
        linhas.append(";".join((pedido, nota.numero, nota.status, chave_para_gerencia(pedido, nota), url_xml, url_pdf)))
    nome = f"{config.CODIGO_EMPRESA_RETORNO}nota_{gerado_em:%d%m%Y%H%M%S}.txt"
    return nome, "".join(linha + "\n" for linha in linhas).encode("utf-8")


def ler_retorno(conteudo: bytes) -> dict[str, dict[str, str]]:
    """Lê um arquivo de retorno (nosso ou do emissor anterior), indexado pelo NR_PEDIDO."""
    registros = {}
    for linha in conteudo.decode("utf-8-sig").splitlines():
        if linha.strip():
            registro = dict(zip(CAMPOS_RETORNO, linha.split(";")))
            registros[registro["NR_PEDIDO"]] = registro
    return registros
