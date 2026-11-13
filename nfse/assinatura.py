"""Certificado A1 e assinatura digital XML no padrão do manual ISS Net (NFS-e nacional v1.01).

RSA-SHA1 (ou SHA-256), canonicalização C14N 1.0, transforms enveloped-signature + C14N e Reference
apontando para o Id do elemento assinado. A <Signature> fica logo depois do elemento, no mesmo pai.

A senha e a chave privada ficam só na memória do processo — nada é gravado em disco.
"""
from __future__ import annotations

import base64
import copy
import hashlib
from datetime import datetime, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree

NS_DS = "http://www.w3.org/2000/09/xmldsig#"
C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
ENVELOPED = "http://www.w3.org/2000/09/xmldsig#enveloped-signature"
# algoritmo -> (SignatureMethod, DigestMethod, função de resumo, hash da assinatura)
ALGORITMOS = {
    "sha1": ("http://www.w3.org/2000/09/xmldsig#rsa-sha1", "http://www.w3.org/2000/09/xmldsig#sha1",
             hashlib.sha1, hashes.SHA1),
    "sha256": ("http://www.w3.org/2001/04/xmldsig-more#rsa-sha256", "http://www.w3.org/2001/04/xmlenc#sha256",
               hashlib.sha256, hashes.SHA256),
}
ALGORITMO_POR_URI = {uri: nome for nome, (uri, _, _, _) in ALGORITMOS.items()}


def _ds(tag: str) -> str:
    return f"{{{NS_DS}}}{tag}"


def _c14n(elemento: etree._Element) -> bytes:
    """C14N 1.0 inclusivo de um elemento (com os namespaces herdados declarados nele).

    Não usar etree.tostring(elemento, method="c14n") direto: no libxml2 2.11 ele inventa xmlns=""
    nos netos de um elemento com namespace padrão herdado (ex.: <prest><CNPJ xmlns="">), e o resumo
    não bate com o do ISS Net/.NET (erro E0714). Canonicalizar uma cópia como documento próprio evita
    o defeito: o tostring do elemento já declara nele os namespaces que ele usa.
    """
    copia = etree.fromstring(etree.tostring(elemento, with_tail=False))
    return etree.tostring(copia, method="c14n")


class CertificadoA1:
    def __init__(self, conteudo_pfx: bytes, senha: str):
        try:
            chave, certificado, cadeia = pkcs12.load_key_and_certificates(conteudo_pfx, senha.encode() or None)
        except ValueError as erro:
            raise ValueError("Não foi possível abrir o certificado: senha incorreta ou arquivo inválido") from erro
        if chave is None or certificado is None:
            raise ValueError("O arquivo .pfx não contém a chave privada e o certificado")
        self.chave = chave
        self.certificado = certificado
        self.cadeia = list(cadeia or [])

    @property
    def titular(self) -> str:
        nomes = self.certificado.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return nomes[0].value if nomes else ""

    @property
    def cnpj(self) -> str:
        """O e-CNPJ traz 'RAZAO SOCIAL:CNPJ' no nome comum."""
        _, _, final = self.titular.rpartition(":")
        return final if final.isdigit() else ""

    @property
    def valido_ate(self) -> datetime:
        return self.certificado.not_valid_after_utc

    @property
    def dias_para_vencer(self) -> int:
        return (self.valido_ate - datetime.now(timezone.utc)).days

    def certificado_base64(self) -> str:
        return base64.b64encode(self.certificado.public_bytes(serialization.Encoding.DER)).decode()

    def pem_certificados(self) -> bytes:
        return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in [self.certificado, *self.cadeia])

    def pem_chave_protegida(self, senha_temporaria: bytes) -> bytes:
        return self.chave.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(senha_temporaria),
        )

    def assinar_bytes(self, dados: bytes, algoritmo: str = "sha1") -> bytes:
        return self.chave.sign(dados, padding.PKCS1v15(), ALGORITMOS[algoritmo][3]())


def assinar(
    alvo: etree._Element,
    certificado: CertificadoA1,
    algoritmo: str = "sha1",
    id_assinatura: str | None = None,
) -> etree._Element:
    """Assina o elemento `alvo` (que precisa ter atributo Id) e insere a <Signature> logo depois dele.

    id_assinatura: atributo Id da <Signature> (o manual do ISS Net lista como obrigatório).
    """
    identificador = alvo.get("Id")
    if not identificador:
        raise ValueError("Elemento a assinar sem atributo Id")
    metodo, metodo_resumo, resumir, _ = ALGORITMOS[algoritmo]
    digest = base64.b64encode(resumir(_c14n(alvo)).digest()).decode()

    assinatura = etree.Element(_ds("Signature"), nsmap={None: NS_DS})
    if id_assinatura:
        assinatura.set("Id", id_assinatura)
    info = etree.SubElement(assinatura, _ds("SignedInfo"))
    etree.SubElement(info, _ds("CanonicalizationMethod"), Algorithm=C14N)
    etree.SubElement(info, _ds("SignatureMethod"), Algorithm=metodo)
    referencia = etree.SubElement(info, _ds("Reference"), URI=f"#{identificador}")
    transforms = etree.SubElement(referencia, _ds("Transforms"))
    etree.SubElement(transforms, _ds("Transform"), Algorithm=ENVELOPED)
    etree.SubElement(transforms, _ds("Transform"), Algorithm=C14N)
    etree.SubElement(referencia, _ds("DigestMethod"), Algorithm=metodo_resumo)
    etree.SubElement(referencia, _ds("DigestValue")).text = digest
    valor = etree.SubElement(assinatura, _ds("SignatureValue"))
    x509_data = etree.SubElement(etree.SubElement(assinatura, _ds("KeyInfo")), _ds("X509Data"))
    etree.SubElement(x509_data, _ds("X509Certificate")).text = certificado.certificado_base64()

    alvo.addnext(assinatura)
    # O SignedInfo é canonicalizado já dentro do documento, com os namespaces herdados.
    valor.text = base64.b64encode(certificado.assinar_bytes(_c14n(info), algoritmo)).decode()
    return assinatura


def verificar(assinatura: etree._Element) -> tuple[bool, bool]:
    """Confere uma <Signature>: devolve (resumo do conteúdo confere, assinatura confere)."""
    raiz = assinatura.getroottree().getroot()
    info = assinatura.find(_ds("SignedInfo"))
    referencia = info.find(_ds("Reference"))
    uri = referencia.get("URI", "")
    if uri.startswith("#"):
        alvo = raiz.xpath("//*[@Id=$id]", id=uri[1:])[0]
        if assinatura in alvo.iterdescendants():
            alvo = copy.deepcopy(alvo)
            for sig in alvo.iter(_ds("Signature")):
                sig.getparent().remove(sig)
    else:
        alvo = copy.deepcopy(raiz)
        for sig in alvo.iter(_ds("Signature")):
            sig.getparent().remove(sig)
    algoritmo = ALGORITMO_POR_URI.get(info.find(_ds("SignatureMethod")).get("Algorithm"), "sha1")
    _, _, resumir, tipo_hash = ALGORITMOS[algoritmo]
    digest = base64.b64encode(resumir(_c14n(alvo)).digest()).decode()
    resumo_ok = digest == referencia.findtext(_ds("DigestValue"))

    certificado = x509.load_der_x509_certificate(
        base64.b64decode(assinatura.findtext(f"{_ds('KeyInfo')}/{_ds('X509Data')}/{_ds('X509Certificate')}"))
    )
    try:
        certificado.public_key().verify(
            base64.b64decode(assinatura.findtext(_ds("SignatureValue"))), _c14n(info), padding.PKCS1v15(), tipo_hash()
        )
        assinatura_ok = True
    except InvalidSignature:
        assinatura_ok = False
    return resumo_ok, assinatura_ok
