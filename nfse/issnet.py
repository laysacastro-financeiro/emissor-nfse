"""Cliente do web service do ISS Net — NFS-e padrão nacional (manual Nota Control v1.01).

SOAP 1.1, document/literal wrapped: cada método recebe dois textos, `nfseCabecMsg` (cabeçalho com a
versão) e `nfseDadosMsg` (o XML do pedido). A conexão é autenticada com o certificado A1 (mTLS).
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from lxml import etree

import config

# Respostas do ISS Net: sem entidades externas, sem rede e sem DTD (defesa contra XML malicioso/XXE).
PARSER_SEGURO = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)

from .assinatura import CertificadoA1
from .nfse_xml import NotaAutorizada, ler_nfse

NS_NFSE = "http://www.sped.fazenda.gov.br/nfse"
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
# Produção do DF: o comunicado da Receita cita nfse.fazenda.df.gov.br; usuários do ACBr citam iss.fazenda.df.gov.br.
# O cliente usa o primeiro endereço cujo WSDL responder com o certificado.
URLS = {
    "homologacao": ["https://nfse.issnetonline.com.br/wsnfsenacional/homologacao/nfse.asmx"],
    "producao": [
        "https://nfse.fazenda.df.gov.br/wsnfsenacional/nfse.asmx",
        "https://iss.fazenda.df.gov.br/wsnfsenacional/nfse.asmx",
    ],
}
# Versão 1.01 = emissão com o grupo IBS/CBS.
CABECALHO = f'<cabecalho versao="1.01" xmlns="{NS_NFSE}"><versaoDados>1.01</versaoDados></cabecalho>'
PASTA_SCHEMAS = config.BASE_DIR / "schemas"
FUSO_BRASILIA = timezone(timedelta(hours=-3))


class ErroComunicacao(Exception):
    """Falha de rede, de certificado ou erro interno do ISS Net — a nota pode ou não ter sido gerada."""


@dataclass
class MensagemRetorno:
    codigo: str
    mensagem: str
    correcao: str = ""

    def __str__(self) -> str:
        texto = f"{self.codigo} - {self.mensagem}".strip(" -")
        return f"{texto} (Correção: {self.correcao})" if self.correcao else texto


@dataclass
class RespostaIssNet:
    xml: bytes
    notas: list[NotaAutorizada] = field(default_factory=list)
    erros: list[MensagemRetorno] = field(default_factory=list)
    alertas: list[MensagemRetorno] = field(default_factory=list)


@lru_cache(maxsize=1)
def _schema() -> etree.XMLSchema:
    arvore = etree.parse(str(PASTA_SCHEMAS / "schema_v101.xsd"))
    # O XSD do ISS Net usa âncoras '^...$' (estilo .NET) em alguns padrões; no XML Schema os padrões já são
    # ancorados e '^'/'$' viram caracteres literais — sem isso até a série 70001 do emissor anterior seria recusada.
    for padrao in arvore.iter("{http://www.w3.org/2001/XMLSchema}pattern"):
        valor = padrao.get("value", "")
        if valor.startswith("^") and valor.endswith("$"):
            padrao.set("value", valor[1:-1])
    return etree.XMLSchema(arvore)


def validar_schema(xml: bytes) -> list[str]:
    """Valida localmente contra o XSD oficial antes de enviar. Devolve a lista de erros (vazia = válido)."""
    schema = _schema()
    if schema.validate(etree.fromstring(xml)):
        return []
    return [f"Linha {e.line}: {e.message}" for e in schema.error_log]


@dataclass
class ContratoWsdl:
    """O que o envelope precisa seguir, lido do WSDL do serviço (com padrão do manual se o WSDL não vier)."""

    namespace: str = NS_NFSE
    parametros_qualificados: bool = False  # elementFormDefault="qualified" -> parâmetros no namespace
    parametros: dict[str, list[str]] = field(default_factory=dict)  # operação -> nomes dos parâmetros
    soap_actions: dict[str, str] = field(default_factory=dict)

    def nomes(self, operacao: str) -> tuple[str, str]:
        nomes = self.parametros.get(operacao) or ["nfseCabecMsg", "nfseDadosMsg"]
        return nomes[0], nomes[1]

    def soap_action(self, operacao: str) -> str:
        return self.soap_actions.get(operacao, f"{self.namespace}/{operacao}")


def ler_wsdl(conteudo: bytes) -> ContratoWsdl:
    ns_xsd, ns_wsdl = "http://www.w3.org/2001/XMLSchema", "http://schemas.xmlsoap.org/wsdl/"
    raiz = etree.fromstring(conteudo, PARSER_SEGURO)
    contrato = ContratoWsdl(namespace=raiz.get("targetNamespace") or NS_NFSE)
    for schema in raiz.iter(f"{{{ns_xsd}}}schema"):
        if schema.get("targetNamespace") == contrato.namespace:
            contrato.parametros_qualificados = schema.get("elementFormDefault") == "qualified"
            for elemento in schema.findall(f"{{{ns_xsd}}}element"):
                nomes = [e.get("name") for e in elemento.iter(f"{{{ns_xsd}}}element") if e is not elemento]
                if len(nomes) >= 2:
                    contrato.parametros[elemento.get("name")] = nomes
    for operacao in raiz.iter(f"{{{ns_wsdl}}}operation"):
        for filho in operacao:
            if filho.tag.endswith("}operation") and filho.get("soapAction"):
                contrato.soap_actions.setdefault(operacao.get("name"), filho.get("soapAction"))
    return contrato


# Como o XML vai dentro dos parâmetros: texto escapado, bloco CDATA ou XML direto. O manual não diz;
# o sistema descobre com o ValidarXml (que não emite nada) e guarda o que o ISS Net aceitou.
FORMATOS = ("xml", "cdata", "texto")
ARQUIVO_FORMATO = config.DADOS_DIR / "issnet_formato.json"


def _conteudo(xml: str, formato: str) -> str:
    if formato == "xml":
        return xml
    if formato == "cdata":
        return f"<![CDATA[{xml}]]>"
    return escape(xml)


@dataclass(frozen=True)
class PerfilEnvio:
    """Variações de assinatura/envelope. O manual não basta para saber qual o ISS Net exige (E0714);
    o diagnóstico testa em ordem, guarda a primeira aceita e as emissões passam a usá-la."""

    nome: str
    id_assinatura: bool  # atributo Id na <Signature> (manual: XS02, ocorrência 1-1)
    estilo_acbr: bool  # <DPS versao="1.01"> sem xmlns + envelope soapenv:/nfse: (como o ACBr)
    tags_explicitas: bool  # <X></X> em vez de <X/> na assinatura
    algoritmo: str  # sha1 (manual) ou sha256
    # Envia só ASCII: acentos viram referências numéricas (&#225;), que no XML são o mesmo caractere.
    # Protege a assinatura se o servidor ler os bytes com outra codificação (ex.: Latin-1).
    transporte_ascii: bool = True
    remover_acentos: bool = False  # último recurso: tira acentos e símbolos do texto antes de assinar


# Testes de 16/10/2026: A–E SEM transporte ASCII foram todos recusados com E0714 (dados/comunicacao).
PERFIS = (
    PerfilEnvio("A", id_assinatura=False, estilo_acbr=False, tags_explicitas=False, algoritmo="sha1"),
    PerfilEnvio("B", id_assinatura=True, estilo_acbr=False, tags_explicitas=False, algoritmo="sha1"),
    PerfilEnvio("C", id_assinatura=True, estilo_acbr=True, tags_explicitas=False, algoritmo="sha1"),
    PerfilEnvio("D", id_assinatura=False, estilo_acbr=False, tags_explicitas=False, algoritmo="sha1",
                remover_acentos=True),
    PerfilEnvio("E", id_assinatura=True, estilo_acbr=True, tags_explicitas=True, algoritmo="sha256",
                remover_acentos=True),
)
PERFIL_POR_NOME = {p.nome: p for p in PERFIS}
ARQUIVO_PERFIL = config.DADOS_DIR / "issnet_perfil.json"


def montar_envelope(
    operacao: str, dados_xml: str, contrato: ContratoWsdl | None = None, formato: str = "texto", prefixado: bool = False
) -> bytes:
    """Monta o envelope SOAP.

    Padrão: só namespaces padrão (sem prefixos), para a infDPS herdar dentro do envelope o mesmo
    contexto de namespaces que tinha quando foi assinada. `prefixado` reproduz o envelope do ACBr
    (soapenv:/nfse:), usado no perfil que imita aquela biblioteca.
    """
    contrato = contrato or ContratoWsdl()
    cabec, dados = contrato.nomes(operacao)
    if prefixado:
        prefixo = "nfse:" if contrato.parametros_qualificados else ""
        return (
            f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:nfse="{contrato.namespace}"><soapenv:Header/>'
            f"<soapenv:Body><nfse:{operacao}><{prefixo}{cabec}>{_conteudo(CABECALHO, formato)}</{prefixo}{cabec}>"
            f"<{prefixo}{dados}>{_conteudo(dados_xml, formato)}</{prefixo}{dados}></nfse:{operacao}>"
            "</soapenv:Body></soapenv:Envelope>"
        ).encode("utf-8")
    sem_ns = "" if contrato.parametros_qualificados else ' xmlns=""'
    return (
        f'<Envelope xmlns="{NS_SOAP}"><Header/><Body><{operacao} xmlns="{contrato.namespace}">'
        f"<{cabec}{sem_ns}>{_conteudo(CABECALHO, formato)}</{cabec}>"
        f"<{dados}{sem_ns}>{_conteudo(dados_xml, formato)}</{dados}>"
        f"</{operacao}></Body></Envelope>"
    ).encode("utf-8")


def extrair_resultado(resposta_soap: bytes) -> bytes:
    """Tira o XML de resposta de dentro do envelope SOAP. Ele pode vir como texto (em <outputXML> ou
    <...Result>) ou como XML direto dentro de <...Response>."""
    try:
        envelope = etree.fromstring(resposta_soap, PARSER_SEGURO)
    except etree.XMLSyntaxError as erro:
        raise ErroComunicacao(f"Resposta do ISS Net não é XML: {resposta_soap[:300]!r}") from erro
    corpo = envelope.find(f"{{{NS_SOAP}}}Body")
    if corpo is None or len(corpo) == 0:
        raise ErroComunicacao("Resposta SOAP sem corpo")
    falha = corpo.find(f"{{{NS_SOAP}}}Fault")
    if falha is not None:
        raise ErroComunicacao(f"Falha SOAP do ISS Net: {falha.findtext('faultstring') or etree.tostring(falha)[:500]!r}")
    resposta = corpo[0]
    if len(resposta) == 0:
        return (resposta.text or "").strip().encode("utf-8")
    filho = resposta[0]
    nome = etree.QName(filho).localname
    if nome == "outputXML" or nome.endswith("Result"):
        if len(filho):
            return etree.tostring(filho[0], encoding="utf-8")
        return (filho.text or "").strip().encode("utf-8")
    return etree.tostring(filho, encoding="utf-8")


def formato_rejeitado(resposta: RespostaIssNet) -> bool:
    """True quando o ISS Net não conseguiu ler o cabeçalho ou a operação (formato errado)."""
    return any(
        m.codigo in ("E183", "E232") or (m.codigo == "E160" and "abeçalho" in m.mensagem) for m in resposta.erros
    )


def _mensagens(raiz: etree._Element, lista: str) -> list[MensagemRetorno]:
    return [
        MensagemRetorno(
            codigo=(m.findtext("{*}Codigo") or "").strip(),
            mensagem=(m.findtext("{*}Mensagem") or "").strip(),
            correcao=(m.findtext("{*}Correcao") or "").strip(),
        )
        for bloco in raiz.iter(f"{{*}}{lista}")
        for m in bloco.iter("{*}MensagemRetorno")
    ]


def interpretar_resposta(xml: bytes) -> RespostaIssNet:
    if not xml:
        raise ErroComunicacao("Resposta vazia do ISS Net")
    raiz = etree.fromstring(xml)
    retorno = _mensagens(raiz, "ListaMensagemRetorno") + _mensagens(raiz, "ListaMensagemRetornoLote")
    # Códigos "S..." são de sucesso (ex.: S000 - XML válido) e vêm na mesma lista dos erros.
    return RespostaIssNet(
        xml=xml,
        notas=ler_nfse(xml, exigir=False),
        erros=[m for m in retorno if not m.codigo.startswith("S")],
        alertas=_mensagens(raiz, "ListaMensagemAlertaRetorno") + [m for m in retorno if m.codigo.startswith("S")],
    )


def _sem_declaracao(xml: bytes) -> str:
    return etree.tostring(etree.fromstring(xml), encoding="unicode")


class ClienteIssNet:
    def __init__(self, certificado: CertificadoA1, ambiente: str = "homologacao", timeout: int = 90):
        if ambiente not in URLS:
            raise ValueError(f"Ambiente desconhecido: {ambiente}")
        if ambiente == "producao" and not config.PERMITIR_PRODUCAO:
            raise PermissionError("Emissão em produção bloqueada neste protótipo (config.PERMITIR_PRODUCAO)")
        self.ambiente = ambiente
        self.url = URLS[ambiente][0]
        self.timeout = timeout
        self.certificado = certificado
        self._contexto = self._contexto_ssl(certificado)
        self.pasta_log = config.DADOS_DIR / "comunicacao"
        self.contrato, self.aviso_wsdl = self._obter_contrato()
        self.wsdl_ok = not self.aviso_wsdl
        self.formato = self._formato_salvo()
        self.perfil = self._perfil_salvo()

    @property
    def tp_amb(self) -> int:
        return 1 if self.ambiente == "producao" else 2

    def _perfil_salvo(self) -> PerfilEnvio | None:
        """Perfil de assinatura/envelope já aceito pelo ISS Net (None = ainda não diagnosticado).
        Produção usa o confirmado na homologação: é o mesmo sistema ISS Net."""
        if ARQUIVO_PERFIL.exists():
            salvos = json.loads(ARQUIVO_PERFIL.read_text("utf-8"))
            return PERFIL_POR_NOME.get(salvos.get(self.ambiente) or salvos.get("homologacao", ""))
        return None

    def salvar_perfil(self, perfil: PerfilEnvio) -> None:
        self.perfil = perfil
        salvos = json.loads(ARQUIVO_PERFIL.read_text("utf-8")) if ARQUIVO_PERFIL.exists() else {}
        salvos[self.ambiente] = perfil.nome
        ARQUIVO_PERFIL.parent.mkdir(parents=True, exist_ok=True)
        ARQUIVO_PERFIL.write_text(json.dumps(salvos), "utf-8")

    def _formato_salvo(self) -> str | None:
        """Formato já confirmado pelo ISS Net em um teste anterior (None = ainda não descoberto)."""
        if ARQUIVO_FORMATO.exists():
            dados = json.loads(ARQUIVO_FORMATO.read_text("utf-8"))
            return dados.get(self.ambiente) or dados.get("homologacao")
        return None

    def descobrir_formato(self, dados_xml: bytes) -> list[str]:
        """Testa os formatos no ValidarXml (não emite nada) e guarda o primeiro que o ISS Net entender."""
        relatorio = []
        for formato in FORMATOS:
            try:
                resposta = self.chamar("ValidarXml", _sem_declaracao(dados_xml), formato)
            except ErroComunicacao as erro:
                relatorio.append(f"{formato}: falha de comunicação ({erro})")
                continue
            mensagens = "; ".join(str(m) for m in resposta.erros) or "sem erros"
            relatorio.append(f"{formato}: {mensagens}")
            if not formato_rejeitado(resposta):
                self.formato = formato
                salvos = json.loads(ARQUIVO_FORMATO.read_text("utf-8")) if ARQUIVO_FORMATO.exists() else {}
                salvos[self.ambiente] = formato
                ARQUIVO_FORMATO.parent.mkdir(parents=True, exist_ok=True)
                ARQUIVO_FORMATO.write_text(json.dumps(salvos), "utf-8")
                break
        return relatorio

    def _obter_contrato(self) -> tuple[ContratoWsdl, str]:
        """Baixa o WSDL (exige o certificado) e guarda uma cópia; define self.url como o primeiro endereço
        que responder. Se nenhum responder, segue o padrão do manual e devolve o aviso."""
        falhas = []
        for url in URLS[self.ambiente]:
            try:
                self._completar_cadeia(url)
            except Exception as erro:  # sem AIA ou sem acesso: segue, a própria conexão dirá se a cadeia fecha
                falhas.append(f"{url}: cadeia não completada ({erro})")
            try:
                with urllib.request.urlopen(f"{url}?WSDL", context=self._contexto, timeout=self.timeout) as resposta:
                    conteudo = resposta.read()
                contrato = ler_wsdl(conteudo)
            except Exception as erro:  # endereço fora do ar, sem permissão ou resposta que não é WSDL
                falhas.append(f"{url}: {erro}")
                continue
            self.url = url
            self.pasta_log.mkdir(parents=True, exist_ok=True)
            (self.pasta_log / f"wsdl_{self.ambiente}.xml").write_bytes(conteudo)
            (self.pasta_log / f"wsdl_{self.ambiente}_endereco.txt").write_text(
                url + ("\nTentativas anteriores: " + " | ".join(falhas) if falhas else ""), "utf-8")
            return contrato, ""
        aviso = "WSDL não lido (" + " | ".join(falhas) + "); usando o formato do manual"
        self.pasta_log.mkdir(parents=True, exist_ok=True)
        (self.pasta_log / f"wsdl_{self.ambiente}_falha.txt").write_text(aviso, "utf-8")
        return ContratoWsdl(), aviso

    def _registrar(self, operacao: str, envio: bytes, resposta: bytes) -> None:
        """Guarda cada troca com o ISS Net (auditoria). Não contém senha nem chave privada."""
        agora = datetime.now(FUSO_BRASILIA)
        pasta = self.pasta_log / agora.strftime("%Y-%m-%d")
        pasta.mkdir(parents=True, exist_ok=True)
        base = pasta / f"{agora:%H%M%S_%f}_{operacao}"
        base.with_name(base.name + "_envio.xml").write_bytes(envio)
        base.with_name(base.name + "_resposta.xml").write_bytes(resposta)

    def _completar_cadeia(self, url: str) -> None:
        """Alguns servidores do DF não enviam o certificado intermediário (ex.: Sectigo OV R36 em
        nfse.fazenda.df.gov.br). Navegadores buscam esse intermediário pelo endereço AIA do próprio
        certificado; aqui fazemos o mesmo. A verificação continua ligada: o intermediário só é aceito
        se fechar a cadeia com uma raiz confiável do sistema."""
        host = urllib.parse.urlsplit(url).hostname
        espiao = ssl.create_default_context()
        espiao.check_hostname = False
        espiao.verify_mode = ssl.CERT_NONE  # só para ler o certificado do servidor; nada é enviado
        with socket.create_connection((host, 443), timeout=self.timeout) as conexao:
            with espiao.wrap_socket(conexao, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        pendentes = [x509.load_der_x509_certificate(der)]
        for _ in range(3):  # sobe até 3 níveis: intermediário, cruzado, ...
            proximos = []
            for certificado in pendentes:
                for emissor in self._escolher_cruzados(self._baixar_emissores(certificado)):
                    self._contexto.load_verify_locations(
                        cadata=emissor.public_bytes(serialization.Encoding.PEM).decode())
                    proximos.append(emissor)
            if not proximos:
                return
            pendentes = proximos

    def _escolher_cruzados(self, emissores: list[x509.Certificate]) -> list[x509.Certificate]:
        """Descarta raízes baixadas da internet (nunca viram confiáveis: têm de estar no sistema) e, quando a
        mesma CA vem em várias versões com assinaturas cruzadas (ex.: Sectigo Root R46 assinada por AAA,
        USERTrust e COMODO), fica só com as assinadas por uma raiz que o sistema já confia. Carregar todas
        faz o OpenSSL escolher uma raiz ausente no Windows e recusar a cadeia (unable to get issuer)."""
        confiaveis = {x509.load_der_x509_certificate(c).subject
                      for c in self._contexto.get_ca_certs(binary_form=True)}
        grupos: dict[x509.Name, list[x509.Certificate]] = {}
        for emissor in emissores:
            if emissor.subject != emissor.issuer:
                grupos.setdefault(emissor.subject, []).append(emissor)
        escolhidos = []
        for versoes in grupos.values():
            escolhidos += [v for v in versoes if v.issuer in confiaveis] or versoes
        return escolhidos

    def _baixar_emissores(self, certificado: x509.Certificate) -> list[x509.Certificate]:
        """Baixa o(s) certificado(s) emissor(es) indicados no AIA (DER, PEM ou pacote PKCS#7 .p7c)."""
        try:
            aia = certificado.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
        except x509.ExtensionNotFound:
            return []
        emissores = []
        for descricao in aia:
            if descricao.access_method != AuthorityInformationAccessOID.CA_ISSUERS:
                continue
            with urllib.request.urlopen(descricao.access_location.value, timeout=self.timeout) as resposta:
                dados = resposta.read()
            if dados.startswith(b"-----BEGIN PKCS7"):
                emissores += pkcs7.load_pem_pkcs7_certificates(dados)
            elif dados.startswith(b"-----"):
                emissores.append(x509.load_pem_x509_certificate(dados))
            else:
                try:
                    emissores.append(x509.load_der_x509_certificate(dados))
                except ValueError:
                    emissores += pkcs7.load_der_pkcs7_certificates(dados)
        return emissores

    @staticmethod
    def _contexto_ssl(certificado: CertificadoA1) -> ssl.SSLContext:
        """O módulo ssl só lê certificado de arquivo: grava em pasta temporária, com a chave
        protegida por uma senha aleatória, carrega e apaga em seguida."""
        contexto = ssl.create_default_context()
        senha = secrets.token_hex(24).encode()
        with tempfile.TemporaryDirectory() as pasta:
            arquivo_cert, arquivo_chave = Path(pasta) / "cert.pem", Path(pasta) / "chave.pem"
            arquivo_cert.write_bytes(certificado.pem_certificados())
            arquivo_chave.write_bytes(certificado.pem_chave_protegida(senha))
            contexto.load_cert_chain(arquivo_cert, arquivo_chave, password=senha)
        return contexto

    def chamar(
        self, operacao: str, dados_xml: str, formato: str | None = None, perfil: PerfilEnvio | None = None
    ) -> RespostaIssNet:
        perfil = perfil or self.perfil or PERFIS[0]
        envelope = montar_envelope(
            operacao, dados_xml, self.contrato, formato or self.formato or FORMATOS[0], prefixado=perfil.estilo_acbr
        )
        if perfil.transporte_ascii:
            envelope = envelope.decode("utf-8").encode("ascii", "xmlcharrefreplace")
        pedido = urllib.request.Request(
            self.url,
            data=envelope,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{self.contrato.soap_action(operacao)}"',
            },
        )
        try:
            with urllib.request.urlopen(pedido, context=self._contexto, timeout=self.timeout) as resposta:
                corpo = resposta.read()
        except urllib.error.HTTPError as erro:
            corpo = erro.read()  # falhas SOAP chegam com HTTP 500 e o detalhe no corpo
            if not corpo:
                self._registrar(operacao, envelope, f"HTTP {erro.code} sem corpo".encode())
                raise ErroComunicacao(f"HTTP {erro.code} do ISS Net") from erro
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as erro:
            self._registrar(operacao, envelope, f"Falha de comunicação: {erro}".encode())
            raise ErroComunicacao(f"Sem comunicação com o ISS Net: {erro}") from erro
        self._registrar(operacao, envelope, corpo)
        return interpretar_resposta(extrair_resultado(corpo))

    def validar_xml(self, dados_xml: bytes) -> RespostaIssNet:
        return self.chamar("ValidarXml", _sem_declaracao(dados_xml))

    def gerar_nfse(self, dps_assinada: bytes, perfil: PerfilEnvio | None = None) -> RespostaIssNet:
        perfil = perfil or self.perfil or PERFIS[0]
        return self.chamar("GerarNfse", envelope_gerar_nfse(dps_assinada, perfil), perfil=perfil)

    def consultar_dados_cadastrais(self) -> tuple[RespostaIssNet, dict]:
        """Só leitura: como o ISS Net (deste ambiente) enxerga o cadastro do prestador."""
        prest = config.PRESTADOR
        resposta = self.chamar(
            "ConsultarDadosCadastrais",
            f'<ConsultarDadosCadastraisEnvio xmlns="{NS_NFSE}"><Prestador><CNPJ>{prest["cnpj"]}</CNPJ>'
            f'<IM>{prest["im"]}</IM></Prestador></ConsultarDadosCadastraisEnvio>',
        )
        cadastro = etree.fromstring(resposta.xml, PARSER_SEGURO).find(".//{*}Cadastro")
        resumo = {}
        if cadastro is not None:
            for campo in ("CNPJ", "IM", "xNome", "StatusCadastro", "EmiteNfse", "enderNac/cMun"):
                resumo[campo] = cadastro.findtext("/".join(f"{{*}}{p}" for p in campo.split("/")), "")
            resumo["Atividades"] = [
                f"{a.findtext('{*}cTribMun', '')} ({a.findtext('{*}pAliq', '')}%)" for a in cadastro.iter("{*}Atividade")
            ]
            resumo["TributacoesPermitidas"] = [t.text for t in cadastro.iter("{*}tribISSQN")]
            resumo["PessoasAutorizadas"] = [p.findtext("{*}RazaoSocial", "") for p in cadastro.iter("{*}PessoasAutorizada")]
        return resposta, resumo

    def consultar_dps_disponivel(self, pagina: int = 1) -> tuple[RespostaIssNet, list[tuple[str, str]]]:
        """Só leitura: séries/números de DPS que o ISS Net reconhece como disponíveis para o prestador."""
        prest = config.PRESTADOR
        resposta = self.chamar(
            "ConsultarDpsDisponivel",
            f'<ConsultarDpsDisponivelEnvio xmlns="{NS_NFSE}"><Prestador><CNPJ>{prest["cnpj"]}</CNPJ>'
            f'<IM>{prest["im"]}</IM></Prestador><Pagina>{int(pagina)}</Pagina></ConsultarDpsDisponivelEnvio>',
        )
        disponiveis = [
            (d.findtext("{*}SerieDPS", ""), d.findtext("{*}NumDPS", ""))
            for d in etree.fromstring(resposta.xml, PARSER_SEGURO).iter("{*}DpsDisponivel")
        ]
        return resposta, disponiveis

    def consultar_url_nfse(self, numero_nfse: str) -> tuple[RespostaIssNet, dict[str, str]]:
        """Só leitura: links oficiais de visualização/autenticidade de uma NFS-e já emitida."""
        prest = config.PRESTADOR
        resposta = self.chamar(
            "ConsultarUrlNfse",
            f'<ConsultarUrlNfseEnvio xmlns="{NS_NFSE}"><Prestador><CNPJ>{prest["cnpj"]}</CNPJ>'
            f'<IM>{prest["im"]}</IM></Prestador><NumeroNfse>{int(numero_nfse)}</NumeroNfse>'
            f"<Pagina>1</Pagina></ConsultarUrlNfseEnvio>",
        )
        links = {}
        item = etree.fromstring(resposta.xml, PARSER_SEGURO).find(".//{*}Links")
        if item is not None:
            for chave, tag in (("visualizacao", "UrlVisualizacaoNfse"), ("autenticidade", "UrlVerificaAutenticidade"),
                               ("nacional", "UrlVisualizacaoNfseNacional")):
                valor = (item.findtext(f"{{*}}{tag}") or "").strip()
                if valor:
                    links[chave] = valor
        return resposta, links

    def baixar(self, url: str) -> tuple[bytes, str]:
        """Baixa um documento (ex.: DANFSe) de um link oficial. Devolve (conteúdo, tipo)."""
        if url.startswith("https://"):
            try:
                self._completar_cadeia(url)
            except Exception:  # sem AIA/sem acesso: a própria conexão dirá se a cadeia fecha
                pass
        pedido = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 EmissorPCP"})
        try:
            with urllib.request.urlopen(pedido, context=self._contexto, timeout=self.timeout) as resposta:
                return resposta.read(), resposta.headers.get("Content-Type", "")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as erro:
            raise ErroComunicacao(f"Não foi possível baixar {url}: {erro}") from erro

    def consultar_nfse_por_dps(self, serie: str, numero: int) -> RespostaIssNet:
        prest = config.PRESTADOR
        return self.chamar(
            "ConsultarNfseDps",  # nome do WSDL (o manual chama de ConsultarNfsePorDps)
            f'<ConsultarNfseDpsEnvio xmlns="{NS_NFSE}"><IdentificacaoDps><NumDPS>{int(numero)}</NumDPS>'
            f"<SerieDPS>{int(serie)}</SerieDPS></IdentificacaoDps><Prestador><CNPJ>{prest['cnpj']}</CNPJ>"
            f"<IM>{prest['im']}</IM></Prestador></ConsultarNfseDpsEnvio>",
        )


def envelope_gerar_nfse(dps_assinada: bytes, perfil: PerfilEnvio | None = None) -> str:
    """XML do GerarNfse. É a última gravação antes do envio: os ajustes de texto do perfil entram aqui."""
    dps = _sem_declaracao(dps_assinada)
    if perfil and perfil.estilo_acbr:
        # Como o ACBr: a DPS perde o xmlns próprio e herda o do <GerarNfseEnvio> (mesmo significado).
        dps = dps.replace(f'<DPS xmlns="{NS_NFSE}" versao="1.01">', '<DPS versao="1.01">', 1)
    if perfil and perfil.tags_explicitas:
        # <X .../> -> <X ...></X> só na assinatura, para o texto enviado já estar na forma canônica (C14N).
        inicio = dps.index("<Signature")
        dps = dps[:inicio] + re.sub(r"<(\w+)([^<>]*?)\s*/>", r"<\1\2></\1>", dps[inicio:])
    return f'<GerarNfseEnvio xmlns="{NS_NFSE}">{dps}</GerarNfseEnvio>'
