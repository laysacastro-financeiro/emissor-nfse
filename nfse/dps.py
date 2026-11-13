"""Montagem da DPS (Declaração de Prestação de Serviço) no padrão nacional, versão 1.01.

A estrutura replica a DPS da NFS-e 770636 emitida pelo ISS Net DF em 14/10/2026.
O ISS Net calcula o valor do ISS e todo o IBS/CBS; a DPS leva só a classificação.
A assinatura digital (certificado A1) entra na etapa de envio, ainda não implementada.
"""
from __future__ import annotations

import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import config

from .remessa import LIMITES, Venda

NS_NFSE = "http://www.sped.fazenda.gov.br/nfse"
VERSAO_DPS = "1.01"
FUSO_BRASILIA = timezone(timedelta(hours=-3))
LIMITE_DESCRICAO = 2000
LIMITE_INF_COMPL = 2000
TP_RET_ISSQN_RETIDO_TOMADOR = "2"


def valor_br(valor: Decimal, casas: int = 2) -> str:
    """1650 -> '1.650,00'."""
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def descricao_servico(venda: Venda) -> str:
    texto = " ".join(p for p in (venda.descricao, venda.detalhe) if p)
    texto += (
        f" - Quantidade: {valor_br(venda.quantidade, 4)} (X) Valor unitário: {valor_br(venda.valor_unitario)}"
        f" (=) Total: R$ {valor_br(venda.valor_total)}"
    )
    return texto[:LIMITE_DESCRICAO]


def composicao_valor(valor: Decimal) -> str:
    """'Valor bruto faturado: R$ 165,00, composto pelo preço líquido de R$ 155,68, acrescido dos tributos...'

    Cada tributo = valor bruto x alíquota, arredondado; preço líquido = valor bruto x (1 - soma das
    alíquotas), arredondado — regra que reproduz os textos de referência da usuária."""
    tributos = [
        (nome, aliquota, (valor * aliquota / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))
        for nome, aliquota in config.TRIBUTOS_FATURAMENTO
    ]
    soma_aliquotas = sum(aliquota for _, aliquota, _ in tributos)
    liquido = (valor * (100 - soma_aliquotas) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
    partes = ", ".join(
        f"{nome} ({format(aliquota.normalize(), 'f').replace('.', ',')}%): R$ {valor_br(valor_tributo)}"
        for nome, aliquota, valor_tributo in tributos
    )
    return (
        f"Valor bruto faturado: R$ {valor_br(valor)}, composto pelo preço líquido de R$ {valor_br(liquido)}, "
        f"acrescido dos tributos incidentes sobre o faturamento: {partes}. "
        "Tributos calculados sobre o valor bruto da operação."
    )


def informacoes_complementares(venda: Venda) -> str:
    return f"{config.TEXTO_INF_COMPL} {composicao_valor(venda.valor_total)}"[:LIMITE_INF_COMPL]


def id_dps(serie: str, numero: int) -> str:
    """'DPS' + município (7) + tipo de inscrição (2 = CNPJ) + CNPJ (14) + série (5) + número (15)."""
    return f"DPS{config.COD_MUNICIPIO_EMISSOR}2{config.PRESTADOR['cnpj']}{int(serie):05d}{int(numero):015d}"


_TROCAS_TIPOGRAFICAS = str.maketrans({
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-", "•": "-", "‘": "'", "’": "'", "‚": ",", "“": '"', "”": '"',
    "„": '"', " ": " ", "​": ""})


def texto_aceito(texto: str) -> str:
    """O esquema da NFS-e só aceita caracteres até U+00FF (Latin-1) nos textos (ex.: xLgr). Cadastros copiados do
    Word trazem travessão, aspas curvas etc., que o ISS Net recusa: troca pelos equivalentes simples e, para o que
    sobrar fora do Latin-1, tira o acento ou descarta."""
    texto = texto.translate(_TROCAS_TIPOGRAFICAS).replace("…", "...")
    if all(ord(c) <= 0xFF for c in texto):
        return texto
    return "".join(
        c if ord(c) <= 0xFF else "".join(x for x in unicodedata.normalize("NFKD", c) if ord(x) < 0x80)
        for c in texto)


def _sub(pai: ET.Element, tag: str, texto: object | None = None) -> ET.Element:
    elemento = ET.SubElement(pai, tag)
    if texto is not None:
        elemento.text = texto_aceito(texto) if isinstance(texto, str) else str(texto)
    return elemento


def montar_dps(
    venda: Venda, serie: str, numero: int, emitida_em: datetime | None = None, tp_amb: int = 2,
    iss_retido: bool = False,
) -> bytes:
    """tp_amb: 1 = produção (nota com valor fiscal), 2 = homologação.
    iss_retido: tomador substituto tributário (EM059) — tpRetISSQN = 2; o valor da nota (vServ) não muda."""
    emitida_em = (emitida_em or datetime.now(FUSO_BRASILIA)).replace(microsecond=0)
    prest_cfg, serv_cfg, ibs_cfg = config.PRESTADOR, config.SERVICO, config.IBSCBS
    cliente = venda.cliente

    dps = ET.Element("DPS", {"xmlns": NS_NFSE, "versao": VERSAO_DPS})
    inf = _sub(dps, "infDPS")
    inf.set("Id", id_dps(serie, numero))
    _sub(inf, "tpAmb", tp_amb)
    _sub(inf, "dhEmi", emitida_em.isoformat())
    _sub(inf, "verAplic", config.VER_APLIC)
    _sub(inf, "serie", int(serie))
    _sub(inf, "nDPS", int(numero))
    _sub(inf, "dCompet", emitida_em.date().isoformat())
    _sub(inf, "tpEmit", "1")
    _sub(inf, "cLocEmi", config.COD_MUNICIPIO_EMISSOR)

    prest = _sub(inf, "prest")
    _sub(prest, "CNPJ", prest_cfg["cnpj"])
    _sub(prest, "IM", prest_cfg["im"])
    _sub(prest, "fone", prest_cfg["fone"])
    _sub(prest, "email", prest_cfg["email"])
    reg_trib = _sub(prest, "regTrib")
    _sub(reg_trib, "opSimpNac", prest_cfg["op_simp_nac"])
    _sub(reg_trib, "regEspTrib", prest_cfg["reg_esp_trib"])

    toma = _sub(inf, "toma")
    _sub(toma, cliente.tipo_documento, cliente.documento)
    _sub(toma, "xNome", cliente.razao_social[: LIMITES["razao_social"]])
    end = _sub(toma, "end")
    end_nac = _sub(end, "endNac")
    _sub(end_nac, "cMun", cliente.cod_municipio)
    _sub(end_nac, "CEP", cliente.cep)
    _sub(end, "xLgr", cliente.logradouro[: LIMITES["logradouro"]])
    _sub(end, "nro", (cliente.numero or "S/N")[: LIMITES["numero"]])
    if cliente.complemento:
        _sub(end, "xCpl", cliente.complemento[: LIMITES["complemento"]])
    _sub(end, "xBairro", cliente.bairro[: LIMITES["bairro"]])

    serv = _sub(inf, "serv")
    loc_prest = _sub(serv, "locPrest")
    _sub(loc_prest, "cLocPrestacao", config.COD_MUNICIPIO_EMISSOR)
    c_serv = _sub(serv, "cServ")
    _sub(c_serv, "cTribNac", serv_cfg["c_trib_nac"])
    _sub(c_serv, "cTribMun", serv_cfg["c_trib_mun"])
    _sub(c_serv, "xDescServ", descricao_servico(venda))
    _sub(c_serv, "cNBS", serv_cfg["c_nbs"])
    info_compl = _sub(serv, "infoCompl")
    _sub(info_compl, "xInfComp", informacoes_complementares(venda))

    valores = _sub(inf, "valores")
    v_serv_prest = _sub(valores, "vServPrest")
    _sub(v_serv_prest, "vServ", f"{venda.valor_total:.2f}")
    trib = _sub(valores, "trib")
    trib_mun = _sub(trib, "tribMun")
    _sub(trib_mun, "tribISSQN", serv_cfg["trib_issqn"])
    _sub(trib_mun, "tpRetISSQN", TP_RET_ISSQN_RETIDO_TOMADOR if iss_retido else serv_cfg["tp_ret_issqn"])
    if serv_cfg.get("informar_aliquota"):
        _sub(trib_mun, "pAliq", f"{serv_cfg['aliquota_iss']:.2f}")
    trib_fed = _sub(trib, "tribFed")
    piscofins = _sub(trib_fed, "piscofins")
    _sub(piscofins, "CST", serv_cfg["cst_piscofins"])
    _sub(piscofins, "tpRetPisCofins", serv_cfg["tp_ret_piscofins"])
    tot_trib = _sub(trib, "totTrib")
    p_tot_trib = _sub(tot_trib, "pTotTrib")
    for tag in ("pTotTribFed", "pTotTribEst", "pTotTribMun"):
        _sub(p_tot_trib, tag, "0.00")

    ibscbs = _sub(inf, "IBSCBS")
    _sub(ibscbs, "finNFSe", ibs_cfg["fin_nfse"])
    _sub(ibscbs, "cIndOp", ibs_cfg["c_ind_op"])
    _sub(ibscbs, "indDest", ibs_cfg["ind_dest"])
    g_ibscbs = _sub(_sub(_sub(ibscbs, "valores"), "trib"), "gIBSCBS")
    _sub(g_ibscbs, "CST", ibs_cfg["cst"])
    _sub(g_ibscbs, "cClassTrib", ibs_cfg["c_class_trib"])

    return ET.tostring(dps, encoding="utf-8", xml_declaration=True)
