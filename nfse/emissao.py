"""Emissão das notas de um lote no ISS Net, à prova de duplicidade.

Regras:
- cada pedido tem um único número de DPS (RegistroDps);
- pedido já autorizado não é enviado de novo;
- se um envio anterior não teve resposta conclusiva, primeiro consulta o ISS Net pela DPS
  e só reenvia se a nota não existir;
- a DPS é validada no XSD oficial antes de sair.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from lxml import etree

import config

from .assinatura import CertificadoA1, assinar
from .dps import FUSO_BRASILIA, montar_dps
from .issnet import (
    PERFIS, ClienteIssNet, ErroComunicacao, PerfilEnvio, RespostaIssNet, envelope_gerar_nfse, validar_schema,
)
from .nfse_xml import ler_nfse
from .numeracao import RegistroDps
from .remessa import Venda

AUTORIZADA = "autorizada"
REJEITADA = "rejeitada"
PENDENTE = "pendente"  # enviada sem resposta conclusiva: consultar antes de reenviar
INVALIDA = "invalida"  # não passou no XSD, nem foi enviada
# Códigos do ISS Net que indicam falha do servidor deles, não problema da DPS (observado em 22/10/2026).
CODIGOS_ERRO_INTERNO = {"E232"}


@dataclass
class Envio:
    pedido: str
    numero_dps: int
    situacao: str
    numero_nfse: str = ""
    mensagens: list[str] = field(default_factory=list)
    atualizado_em: str = ""
    serie: str = ""  # série usada nesta tentativa (a série do lote pode mudar entre tentativas)


class ControleEnvios:
    """Situação de cada pedido do lote, gravada em envios.json na pasta do lote."""

    def __init__(self, pasta_lote: Path):
        self.arquivo = Path(pasta_lote) / "envios.json"
        dados = json.loads(self.arquivo.read_text("utf-8")) if self.arquivo.exists() else {}
        self.envios = {pedido: Envio(**e) for pedido, e in dados.items()}

    def registrar(self, envio: Envio) -> None:
        envio.atualizado_em = datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds")
        self.envios[envio.pedido] = envio
        temporario = self.arquivo.with_suffix(".tmp")
        temporario.write_text(
            json.dumps({p: asdict(e) for p, e in self.envios.items()}, ensure_ascii=False, indent=1), "utf-8"
        )
        temporario.replace(self.arquivo)


SUBSTITUICOES = str.maketrans({"°": "o", "º": "o", "ª": "a", "`": "'", "´": "'"})


def sem_acentos(texto: str) -> str:
    """'Valor unitário n° d`agua' -> 'Valor unitario no d'agua' (só ASCII)."""
    decomposto = unicodedata.normalize("NFKD", texto.translate(SUBSTITUICOES))
    return "".join(ch for ch in decomposto if not unicodedata.combining(ch)).encode("ascii", "ignore").decode()


def dps_assinada(
    venda: Venda,
    numero: int,
    certificado: CertificadoA1,
    emitida_em: datetime,
    perfil: PerfilEnvio = PERFIS[0],
    tp_amb: int = 2,
    serie: str | None = None,
    iss_retido: bool = False,
) -> bytes:
    raiz = etree.fromstring(montar_dps(venda, serie or config.SERIE_DPS, numero, emitida_em, tp_amb, iss_retido))
    if perfil.remover_acentos:
        for elemento in raiz.iter():
            if elemento.text:
                elemento.text = sem_acentos(elemento.text)
    inf_dps = raiz.find("{*}infDPS")
    assinar(
        inf_dps,
        certificado,
        algoritmo=perfil.algoritmo,
        id_assinatura=f"Ass_{inf_dps.get('Id')}" if perfil.id_assinatura else None,
    )
    return etree.tostring(raiz, encoding="utf-8", xml_declaration=True)


def _salvar_notas(resposta: RespostaIssNet, pasta_lote: Path, pedido: str) -> str:
    pasta = Path(pasta_lote) / "notas"
    pasta.mkdir(exist_ok=True)
    numero = resposta.notas[0].numero
    (pasta / f"nfse_{numero}_pedido_{pedido}.xml").write_bytes(resposta.xml)
    return numero


def emitir_venda(
    venda: Venda,
    cliente: ClienteIssNet,
    registro: RegistroDps,
    controle: ControleEnvios,
    pasta_lote: Path,
    perfil: PerfilEnvio | None = None,
    serie: str | None = None,
    iss_retido: bool = False,
) -> Envio:
    """`registro` precisa ser o da `serie` (padrão: config.SERIE_DPS). `iss_retido`: tomador substituto tributário."""
    serie = serie or config.SERIE_DPS
    anterior = controle.envios.get(venda.pedido)
    if anterior and anterior.situacao == AUTORIZADA:
        return anterior

    # Envio anterior sem resposta conclusiva: consulta pela MESMA série/número daquele envio antes de
    # qualquer reenvio (mesmo que a série do lote tenha mudado depois).
    erro_interno_anterior = bool(
        anterior and anterior.situacao == REJEITADA
        and any(m.split(" ")[0] in CODIGOS_ERRO_INTERNO for m in anterior.mensagens)
    )  # rejeitadas por E232 antes de esse erro passar a valer como "pendente"
    if anterior and (anterior.situacao == PENDENTE or erro_interno_anterior):
        encontrado = _consultar_existente(venda, cliente, controle, pasta_lote,
                                          anterior.serie or serie, anterior.numero_dps)
        if encontrado:
            return encontrado

    # E0014 (DPS já virou NFS-e) com o mesmo número ainda reservado ao pedido: não adianta reenviar — descobre de
    # quem é a nota que ocupa o número.
    if (anterior and anterior.situacao == REJEITADA and _e0014(anterior.mensagens)
            and registro.pedido_do_numero(anterior.numero_dps) == venda.pedido):
        return _tratar_dps_ja_existente(venda, cliente, registro, controle, pasta_lote, anterior.serie or serie,
                                        anterior.numero_dps)

    # Pedido que já tinha número de DPS nesta série sem envio registrado neste lote (veio em outra
    # remessa/lote): pode já ter virado nota — consulta antes.
    numerado_em_outro_lote = anterior is None and bool(registro.ja_numerados([venda.pedido]))
    numero = registro.numeros_para([venda.pedido])[venda.pedido]
    if numerado_em_outro_lote:
        encontrado = _consultar_existente(venda, cliente, controle, pasta_lote, serie, numero)
        if encontrado:
            return encontrado

    envio = _enviar(venda, numero, cliente, controle, pasta_lote, perfil or cliente.perfil or PERFIS[0], serie, iss_retido)
    if envio.situacao == REJEITADA and _e0014(envio.mensagens):
        return _tratar_dps_ja_existente(venda, cliente, registro, controle, pasta_lote, serie, numero)
    return envio


def _e0014(mensagens: list[str]) -> bool:
    return any(m.startswith("E0014") for m in mensagens)


def _tratar_dps_ja_existente(
    venda: Venda, cliente: ClienteIssNet, registro: RegistroDps, controle: ControleEnvios, pasta_lote: Path,
    serie: str, numero: int,
) -> Envio:
    """O ISS Net respondeu E0014: já existe NFS-e com esta série/número de DPS. Consulta (só leitura) e decide:
    - é a nota deste pedido (mesmo tomador, valor e boleto): fica autorizada, sem reenviar;
    - é de outro pedido: o número é descartado e o pedido precisa de outro (Usar série autorizada);
    - não dá para ter certeza: não mexe em nada e pede conferência no ISS Net."""
    def rejeitar(*mensagens: str) -> Envio:
        envio = Envio(venda.pedido, numero, REJEITADA, mensagens=list(mensagens), serie=serie)
        controle.registrar(envio)
        return envio

    inicio = f"E0014 - a DPS {serie}/{numero} já virou NFS-e no ISS Net"
    try:
        consulta = cliente.consultar_nfse_por_dps(serie, numero)
    except ErroComunicacao as erro:
        return rejeitar(f"{inicio}, mas a consulta da nota falhou ({erro}). Clique em Emitir de novo mais tarde.")
    if not consulta.notas:
        return rejeitar(f"{inicio}, mas a consulta não devolveu a nota. Confira no portal do ISS Net ou procure a Prefeitura.")
    nota = consulta.notas[0]
    mesmo_tomador_e_valor = nota.documento_tomador == venda.cliente.documento and nota.valor_servico == venda.valor_total
    boleto_nota, boleto_venda = nota.boleto, venda.boleto
    if mesmo_tomador_e_valor and boleto_nota and boleto_nota == boleto_venda:
        envio = Envio(venda.pedido, numero, AUTORIZADA, _salvar_notas(consulta, pasta_lote, venda.pedido),
                      [f"Nota já existia no ISS Net (DPS {serie}/{numero}; mesmo tomador, valor e boleto {boleto_venda}) "
                       "— não foi reenviada"], serie=serie)
        controle.registrar(envio)
        return envio
    descricao_nota = (f"NFS-e {nota.numero} de {nota.nome_tomador} ({nota.documento_tomador}), "
                      f"R$ {nota.valor_servico:.2f}, boleto {boleto_nota or 'não informado'}")
    if mesmo_tomador_e_valor and not (boleto_nota and boleto_venda):
        return rejeitar(f"{inicio}: {descricao_nota}. Mesmo tomador e valor, mas sem boleto para conferir se é este "
                        f"pedido ({venda.pedido}). Confira no ISS Net antes de emitir de novo.")
    registro.descartar(venda.pedido, f"ocupado pela {descricao_nota}")
    return rejeitar(f"{inicio}, mas é de outro pedido ({descricao_nota}). O número {numero} foi descartado: clique em "
                    "\"Usar série autorizada\" para reservar outro número e depois em Emitir.")


def _consultar_existente(
    venda: Venda, cliente: ClienteIssNet, controle: ControleEnvios, pasta_lote: Path, serie: str, numero: int
) -> Envio | None:
    """Devolve o Envio quando a nota já existe ou quando não foi possível confirmar (fica pendente)."""
    try:
        consulta = cliente.consultar_nfse_por_dps(serie, numero)
    except ErroComunicacao as erro:
        envio = Envio(venda.pedido, numero, PENDENTE, mensagens=[f"Consulta antes do reenvio falhou: {erro}"], serie=serie)
        controle.registrar(envio)
        return envio
    if consulta.notas:
        envio = Envio(venda.pedido, numero, AUTORIZADA, _salvar_notas(consulta, pasta_lote, venda.pedido),
                      ["Nota já existia no ISS Net (localizada pela DPS) — não foi reenviada"], serie=serie)
        controle.registrar(envio)
        return envio
    return None


MARGEM_RELOGIO_ISS_NET = timedelta(minutes=3)


def _hora_de_emissao() -> datetime:
    """Hora gravada na DPS (dhEmi). O ISS Net recusa com E0008 DPS 'do futuro', e alguns dos servidores dele estão
    com o relógio atrasado em relação ao do PC (em 10/11/2026 ~30% dos envios caíram em E0008, com o relógio do PC
    certo). Por isso a DPS leva a hora de alguns minutos atrás — nunca antes da meia-noite, para não mudar o dia
    de competência (dCompet)."""
    agora = datetime.now(FUSO_BRASILIA)
    meia_noite = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(agora - MARGEM_RELOGIO_ISS_NET, meia_noite)


def _enviar(
    venda: Venda,
    numero: int,
    cliente: ClienteIssNet,
    controle: ControleEnvios,
    pasta_lote: Path,
    perfil: PerfilEnvio,
    serie: str,
    iss_retido: bool = False,
) -> Envio:
    xml = dps_assinada(venda, numero, cliente.certificado, _hora_de_emissao(), perfil, cliente.tp_amb, serie,
                       iss_retido)
    (Path(pasta_lote) / "dps_assinadas").mkdir(exist_ok=True)
    (Path(pasta_lote) / "dps_assinadas" / f"DPS_{serie}_{numero:015d}_pedido_{venda.pedido}.xml").write_bytes(xml)
    erros_schema = validar_schema(envelope_gerar_nfse(xml, perfil).encode("utf-8"))
    if erros_schema:
        envio = Envio(venda.pedido, numero, INVALIDA, mensagens=erros_schema[:10], serie=serie)
        controle.registrar(envio)
        return envio

    # Marca como pendente ANTES de enviar: se cair a conexão, a próxima tentativa consulta primeiro.
    controle.registrar(Envio(venda.pedido, numero, PENDENTE, mensagens=["Enviada, aguardando resposta"], serie=serie))
    try:
        resposta = cliente.gerar_nfse(xml, perfil)
    except ErroComunicacao as erro:
        envio = Envio(venda.pedido, numero, PENDENTE, mensagens=[str(erro)], serie=serie)
        controle.registrar(envio)
        return envio

    if resposta.notas:
        envio = Envio(venda.pedido, numero, AUTORIZADA, _salvar_notas(resposta, pasta_lote, venda.pedido),
                      [str(a) for a in resposta.alertas], serie=serie)
    else:
        mensagens = [str(m) for m in resposta.erros] or ["ISS Net não devolveu nota nem mensagem de erro"]
        # Erro interno do ISS Net (E232 "erro no processamento do arquivo"): a nota pode ou não ter sido
        # gerada. Fica PENDENTE para a próxima tentativa consultar o ISS Net antes de reenviar.
        erro_interno = any(m.codigo in CODIGOS_ERRO_INTERNO for m in resposta.erros)
        envio = Envio(venda.pedido, numero, PENDENTE if erro_interno else REJEITADA,
                      mensagens=mensagens, serie=serie)
    controle.registrar(envio)
    return envio


URL_DANFSE_ADN = "https://adn.nfse.gov.br/danfse/{chave}"  # Ambiente Nacional; exige certificado (mTLS)
URL_CONSULTA_PUBLICA = "https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave={chave}"


def _chave_da_nota(pasta_lote: Path, envio: Envio) -> str:
    for arquivo in (Path(pasta_lote) / "notas").glob(f"nfse_{envio.numero_nfse}_*.xml"):
        for nota in ler_nfse(arquivo.read_bytes(), exigir=False):
            if nota.numero == envio.numero_nfse and nota.chave:
                return nota.chave
    return ""


def buscar_documentos(cliente: ClienteIssNet, controle: ControleEnvios, pasta_lote: Path) -> list[str]:
    """Para cada nota autorizada do lote, só leitura: pede os links oficiais ao ISS Net e baixa o PDF
    (DANFSe). Se o ISS Net não fornecer os links (o DF recusa com L090), baixa a DANFSe do Ambiente
    Nacional pela chave de acesso. Grava links.json e a pasta pdfs/ no lote. Relatório por nota."""
    pasta_lote = Path(pasta_lote)
    arquivo_links = pasta_lote / "links.json"
    todos = json.loads(arquivo_links.read_text("utf-8")) if arquivo_links.exists() else {}
    (pasta_lote / "pdfs").mkdir(exist_ok=True)
    relatorio = []
    for envio in controle.envios.values():
        if envio.situacao != AUTORIZADA or not envio.numero_nfse:
            continue
        links, tentativas = {}, []
        try:
            resposta, links = cliente.consultar_url_nfse(envio.numero_nfse)
            if not links:
                tentativas.append("ISS Net sem links: " + "; ".join(m.codigo for m in resposta.erros))
        except ErroComunicacao as erro:
            tentativas.append(f"ISS Net: {erro}")
        chave_acesso = _chave_da_nota(pasta_lote, envio)
        if chave_acesso:
            links.setdefault("adn", URL_DANFSE_ADN.format(chave=chave_acesso))
            links.setdefault("consulta_publica", URL_CONSULTA_PUBLICA.format(chave=chave_acesso))
        registro = {"pedido": envio.pedido, "chave": chave_acesso, **links, "pdf": "", "observacao": ""}
        for chave in ("visualizacao", "nacional", "adn"):
            url = links.get(chave)
            if not url:
                continue
            try:
                conteudo, tipo = cliente.baixar(url)
            except ErroComunicacao as erro:
                tentativas.append(f"{chave}: {erro}")
                continue
            if conteudo.startswith(b"%PDF"):
                nome = f"NFSe_{envio.numero_nfse}_pedido_{envio.pedido}.pdf"
                (pasta_lote / "pdfs" / nome).write_bytes(conteudo)
                registro["pdf"] = nome
                break
            tentativas.append(f"{chave}: não é PDF ({tipo or 'tipo desconhecido'}, {len(conteudo)} bytes)")
        registro["observacao"] = " | ".join(tentativas)
        todos[envio.numero_nfse] = registro
        relatorio.append(f"NFS-e {envio.numero_nfse}: " + (f"PDF salvo ({registro['pdf']})" if registro["pdf"]
                                                           else "PDF não baixado — " + registro["observacao"]))
    arquivo_links.write_text(json.dumps(todos, ensure_ascii=False, indent=1), "utf-8")
    return relatorio


def testar_series(
    venda: Venda,
    cliente: ClienteIssNet,
    registro_da_serie,
    controle: ControleEnvios,
    pasta_lote: Path,
    candidatas: list[str],
) -> tuple[str | None, list[str]]:
    """Envia UMA venda tentando as séries em ordem e devolve a primeira que o ISS Net não recusar por
    série (EM016/E0010). Para na hora em autorização, falha de comunicação ou erro de outro tipo —
    assim nunca há mais de uma nota para a venda. `registro_da_serie(serie)` devolve o RegistroDps."""
    relatorio = []
    for serie in candidatas:
        envio = emitir_venda(venda, cliente, registro_da_serie(serie), controle, pasta_lote, serie=serie)
        relatorio.append(f"Série {serie}: {envio.situacao} — {'; '.join(envio.mensagens) or 'ok'}")
        recusada_por_serie = any(m.startswith(("EM016", "E0010")) for m in envio.mensagens)
        if envio.situacao == REJEITADA and recusada_por_serie:
            continue
        aceita = envio.situacao == AUTORIZADA or envio.situacao == REJEITADA
        return (serie if aceita else None), relatorio
    return None, relatorio


def diagnosticar_assinatura(
    venda: Venda, cliente: ClienteIssNet, registro: RegistroDps, controle: ControleEnvios, pasta_lote: Path
) -> list[str]:
    """Envia UMA venda testando os perfis em ordem e guarda o primeiro que o ISS Net não recusar por
    assinatura (E0714). Para na hora em caso de falha de comunicação, para não arriscar duplicidade."""
    relatorio = []
    for perfil in PERFIS:
        envio = emitir_venda(venda, cliente, registro, controle, pasta_lote, perfil)
        relatorio.append(f"Perfil {perfil.nome}: {envio.situacao} — {'; '.join(envio.mensagens) or 'ok'}")
        if envio.situacao in (PENDENTE, INVALIDA):
            break
        if envio.situacao == AUTORIZADA or not any(m.startswith("E0714") for m in envio.mensagens):
            cliente.salvar_perfil(perfil)
            break
    return relatorio
