"""Assinatura, schema e cliente do ISS Net — sem rede, com um certificado de teste gerado na hora.

Rodar na pasta emissor_nfse:  python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nfse import emissao  # noqa: E402
from nfse.assinatura import NS_DS, CertificadoA1, verificar  # noqa: E402
from nfse.dps import FUSO_BRASILIA  # noqa: E402
from nfse.issnet import (  # noqa: E402
    PERFIS, ClienteIssNet, ErroComunicacao, envelope_gerar_nfse, extrair_resultado, formato_rejeitado, interpretar_resposta,
    ler_wsdl, montar_envelope, validar_schema,
)
from nfse.numeracao import RegistroDps  # noqa: E402
from test_prototipo import FIX, venda_exemplo  # noqa: E402


def pfx_de_teste(senha: str = "teste123") -> bytes:
    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EMPRESA DE TESTE LTDA:11222333000181")])
    agora = datetime.now(timezone.utc)
    certificado = (
        x509.CertificateBuilder().subject_name(nome).issuer_name(nome).public_key(chave.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(agora - timedelta(days=1))
        .not_valid_after(agora + timedelta(days=365)).sign(chave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"teste", chave, certificado, None, serialization.BestAvailableEncryption(senha.encode())
    )


CERTIFICADO = CertificadoA1(pfx_de_teste(), "teste123")
EMITIDA = datetime(2026, 10, 16, 15, 0, 0, tzinfo=FUSO_BRASILIA)


def resposta_soap(operacao: str, xml_resultado: str) -> bytes:
    from xml.sax.saxutils import escape
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        f'<{operacao}Response xmlns="http://www.sped.fazenda.gov.br/nfse"><{operacao}Result>{escape(xml_resultado)}'
        f"</{operacao}Result></{operacao}Response></soap:Body></soap:Envelope>"
    ).encode()


def resposta_com_nota() -> str:
    """GerarNfseResposta montada com a NFS-e real 770636."""
    real = etree.parse(str(FIX / "nfse_770636.xml")).getroot()
    nfse = real.find(".//{*}NFSe")
    return (
        '<GerarNfseResposta xmlns="http://www.sped.fazenda.gov.br/nfse"><ListaNfse><CompNfse>'
        f"{etree.tostring(nfse, encoding='unicode')}</CompNfse></ListaNfse></GerarNfseResposta>"
    )


RESPOSTA_ERRO = (
    '<GerarNfseResposta xmlns="http://www.sped.fazenda.gov.br/nfse"><ListaMensagemRetorno><MensagemRetorno>'
    "<Codigo>E0123</Codigo><Mensagem>CEP do tomador inválido</Mensagem><Correcao>Informe o CEP</Correcao>"
    "</MensagemRetorno></ListaMensagemRetorno></GerarNfseResposta>"
)


class TestCertificado(unittest.TestCase):
    def test_dados_do_certificado(self):
        self.assertEqual(CERTIFICADO.cnpj, "11222333000181")
        self.assertGreater(CERTIFICADO.dias_para_vencer, 300)

    def test_senha_errada(self):
        with self.assertRaises(ValueError):
            CertificadoA1(pfx_de_teste("certa"), "errada")


class TestCanonicalizacao(unittest.TestCase):
    def test_sem_xmlns_vazio_inventado(self):
        # Defeito do libxml2 2.11 que causava o E0714: <CNPJ xmlns=""> nos netos do elemento assinado.
        from nfse.assinatura import _c14n
        raiz = etree.fromstring(b'<DPS xmlns="urn:x" v="1"><inf Id="a"><prest><CNPJ>1</CNPJ></prest></inf>tail</DPS>')
        self.assertEqual(_c14n(raiz[0]), b'<inf xmlns="urn:x" Id="a"><prest><CNPJ>1</CNPJ></prest></inf>')

    def test_dps_assinada_sem_xmlns_vazio(self):
        from nfse.assinatura import _c14n
        raiz = etree.fromstring(emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA))
        self.assertNotIn(b'xmlns=""', _c14n(raiz.find("{*}infDPS")))
        self.assertNotIn(b'xmlns=""', _c14n(raiz.find(f".//{{{NS_DS}}}SignedInfo")))


class TestAssinatura(unittest.TestCase):
    def setUp(self):
        self.xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA)
        self.raiz = etree.fromstring(self.xml)

    def test_assinatura_confere(self):
        assinatura = self.raiz.find(f"{{{NS_DS}}}Signature")
        self.assertEqual(assinatura.getprevious().tag.split("}")[1], "infDPS")
        self.assertEqual(verificar(assinatura), (True, True))

    def test_alteracao_depois_de_assinar_e_detectada(self):
        self.raiz.find(".//{*}vServ").text = "999.00"
        self.assertEqual(verificar(self.raiz.find(f"{{{NS_DS}}}Signature")), (False, True))

    def test_assinatura_confere_dentro_do_envelope_soap(self):
        # O ISS Net confere a assinatura com a DPS já dentro do envelope (C14N inclui namespaces herdados).
        envelope = etree.fromstring(montar_envelope("GerarNfse", envelope_gerar_nfse(self.xml), formato="xml"))
        assinatura = envelope.find(f".//{{{NS_DS}}}Signature")
        self.assertEqual(verificar(assinatura), (True, True))

    def test_dps_assinada_passa_no_schema_oficial(self):
        self.assertEqual(validar_schema(envelope_gerar_nfse(self.xml).encode()), [])


class TestPerfis(unittest.TestCase):
    def test_todos_os_perfis_assinam_valido_e_passam_no_schema(self):
        for perfil in PERFIS:
            xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA, perfil)
            dados = envelope_gerar_nfse(xml, perfil)
            self.assertEqual(validar_schema(dados.encode()), [], perfil.nome)
            envelope = etree.fromstring(montar_envelope("GerarNfse", dados, formato="xml", prefixado=perfil.estilo_acbr))
            assinatura = envelope.find(f".//{{{NS_DS}}}Signature")
            # Conferida como o ISS Net recebe: a DPS isolada dentro do GerarNfseEnvio.
            isolada = etree.fromstring(dados.encode()).find(f".//{{{NS_DS}}}Signature")
            self.assertEqual(verificar(isolada), (True, True), perfil.nome)
            self.assertEqual(assinatura.get("Id") is not None, perfil.id_assinatura, perfil.nome)

    def test_transporte_ascii_mantem_assinatura_valida(self):
        xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA)
        envelope = montar_envelope("GerarNfse", envelope_gerar_nfse(xml), formato="xml")
        ascii_ = envelope.decode("utf-8").encode("ascii", "xmlcharrefreplace")
        self.assertIn(b"unit&#225;rio", ascii_)
        assinatura = etree.fromstring(ascii_).find(f".//{{{NS_DS}}}Signature")
        self.assertEqual(verificar(assinatura), (True, True))

    def test_sem_acentos(self):
        self.assertEqual(emissao.sem_acentos("Valor unitário n° d`agua Luíza"), "Valor unitario no d'agua Luiza")
        xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA, PERFIS[3])
        self.assertTrue(all(b < 128 for b in xml))

    def test_perfil_acbr_tira_xmlns_da_dps(self):
        perfil = PERFIS[-1]
        xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA, perfil)
        dados = envelope_gerar_nfse(xml, perfil)
        self.assertNotIn("/>", dados[dados.index("<Signature"):])
        self.assertIn('<GerarNfseEnvio xmlns="http://www.sped.fazenda.gov.br/nfse"><DPS versao="1.01">', dados)
        self.assertIn("</CanonicalizationMethod>", dados)
        self.assertIn("rsa-sha256", dados)


class TestSoap(unittest.TestCase):
    def test_envelope_no_formato_do_manual(self):
        envelope = etree.fromstring(montar_envelope("GerarNfse", "<a>1</a>"))
        self.assertEqual(envelope.tag, "{http://schemas.xmlsoap.org/soap/envelope/}Envelope")
        metodo = envelope.find(".//{http://www.sped.fazenda.gov.br/nfse}GerarNfse")
        self.assertIn("<versaoDados>1.01</versaoDados>", metodo.findtext("nfseCabecMsg"))
        self.assertEqual(metodo.findtext("nfseDadosMsg"), "<a>1</a>")

    def test_formatos_do_parametro(self):
        for formato in ("cdata", "texto"):
            envelope = etree.fromstring(montar_envelope("ValidarXml", "<a>1</a>", formato=formato))
            self.assertEqual(envelope.find(".//nfseDadosMsg").text, "<a>1</a>", formato)
        envelope = etree.fromstring(montar_envelope("ValidarXml", "<a>1</a>", formato="xml"))
        filho = envelope.find(".//nfseDadosMsg/a")
        self.assertEqual((filho.tag, filho.text), ("a", "1"))

    def test_resposta_xml_direto_do_issnet(self):
        # Formato real devolvido pelo ValidarXml da homologação em 16/10/2026.
        soap = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                '<ValidarXmlResponse xmlns="http://www.sped.fazenda.gov.br/nfse"><ValidarXmlResposta>'
                "<ListaMensagemRetorno><MensagemRetorno><Codigo>E232</Codigo><Mensagem>Operação não identificada"
                "</Mensagem></MensagemRetorno></ListaMensagemRetorno></ValidarXmlResposta></ValidarXmlResponse>"
                "</s:Body></s:Envelope>").encode()
        resposta = interpretar_resposta(extrair_resultado(soap))
        self.assertEqual(resposta.erros[0].codigo, "E232")
        self.assertTrue(formato_rejeitado(resposta))

    def test_resposta_com_nota(self):
        resposta = interpretar_resposta(extrair_resultado(resposta_soap("GerarNfse", resposta_com_nota())))
        self.assertEqual([n.numero for n in resposta.notas], ["770636"])
        self.assertEqual(resposta.erros, [])

    def test_resposta_com_erro(self):
        resposta = interpretar_resposta(extrair_resultado(resposta_soap("GerarNfse", RESPOSTA_ERRO)))
        self.assertEqual(resposta.notas, [])
        self.assertEqual(str(resposta.erros[0]), "E0123 - CEP do tomador inválido (Correção: Informe o CEP)")

    def test_falha_soap(self):
        falha = (b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><soap:Fault>'
                 b"<faultcode>soap:Server</faultcode><faultstring>BEGIN e COMMIT</faultstring></soap:Fault>"
                 b"</soap:Body></soap:Envelope>")
        with self.assertRaises(ErroComunicacao):
            extrair_resultado(falha)

    def test_envelope_segue_o_wsdl(self):
        wsdl = b"""<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/" xmlns:s="http://www.w3.org/2001/XMLSchema"
          xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/" targetNamespace="http://www.sped.fazenda.gov.br/nfse">
          <wsdl:types><s:schema elementFormDefault="qualified" targetNamespace="http://www.sped.fazenda.gov.br/nfse">
            <s:element name="GerarNfse"><s:complexType><s:sequence>
              <s:element minOccurs="0" maxOccurs="1" name="nfseCabecMsg" type="s:string"/>
              <s:element minOccurs="0" maxOccurs="1" name="nfseDadosMsg" type="s:string"/>
            </s:sequence></s:complexType></s:element></s:schema></wsdl:types>
          <wsdl:binding name="b"><wsdl:operation name="GerarNfse">
            <soap:operation soapAction="http://www.sped.fazenda.gov.br/nfse/GerarNfse" style="document"/>
          </wsdl:operation></wsdl:binding></wsdl:definitions>"""
        contrato = ler_wsdl(wsdl)
        self.assertTrue(contrato.parametros_qualificados)
        self.assertEqual(contrato.soap_action("GerarNfse"), "http://www.sped.fazenda.gov.br/nfse/GerarNfse")
        envelope = etree.fromstring(montar_envelope("GerarNfse", "<a/>", contrato))
        self.assertIsNotNone(envelope.find(".//{http://www.sped.fazenda.gov.br/nfse}nfseDadosMsg"))

    def test_producao_bloqueada_sem_autorizacao(self):
        with mock.patch("config.PERMITIR_PRODUCAO", False), self.assertRaises(PermissionError):
            ClienteIssNet(CERTIFICADO, "producao")

    def test_dps_de_producao_tem_tpamb_1(self):
        xml = emissao.dps_assinada(venda_exemplo(), 1, CERTIFICADO, EMITIDA, tp_amb=1)
        self.assertEqual(etree.fromstring(xml).findtext(".//{*}tpAmb"), "1")
        self.assertEqual(validar_schema(envelope_gerar_nfse(xml).encode()), [])


class TestEmissaoSemDuplicidade(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        self.lote = Path(self.pasta.name)
        self.registro = RegistroDps(self.lote / "num.json", "70002")
        self.cliente = mock.Mock(spec=ClienteIssNet)
        self.cliente.certificado = CERTIFICADO
        self.cliente.perfil = PERFIS[0]
        self.cliente.tp_amb = 2
        self.nota = interpretar_resposta(resposta_com_nota().encode())

    def tearDown(self):
        self.pasta.cleanup()

    def emitir(self):
        return emissao.emitir_venda(venda_exemplo(), self.cliente, self.registro, emissao.ControleEnvios(self.lote), self.lote)

    def test_autorizada_nao_e_reenviada(self):
        self.cliente.gerar_nfse.return_value = self.nota
        self.assertEqual(self.emitir().situacao, emissao.AUTORIZADA)
        self.assertEqual(self.emitir().situacao, emissao.AUTORIZADA)
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)
        self.assertEqual(len(list((self.lote / "notas").glob("*.xml"))), 1)

    def test_queda_de_conexao_consulta_antes_de_reenviar(self):
        self.cliente.gerar_nfse.side_effect = ErroComunicacao("timeout")
        self.assertEqual(self.emitir().situacao, emissao.PENDENTE)
        # A nota tinha sido gerada no ISS Net apesar da queda: a consulta acha e não reenvia.
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        envio = self.emitir()
        self.assertEqual((envio.situacao, envio.numero_nfse), (emissao.AUTORIZADA, "770636"))
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)

    def test_pedido_numerado_em_outro_lote_consulta_antes(self):
        # O mesmo pedido já recebeu número de DPS em outro lote: pode já ter nota — consulta primeiro.
        self.registro.numeros_para([venda_exemplo().pedido])
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        envio = self.emitir()
        self.assertEqual((envio.situacao, envio.numero_nfse), (emissao.AUTORIZADA, "770636"))
        self.cliente.gerar_nfse.assert_not_called()

    def test_testar_series_para_na_primeira_aceita(self):
        serie_invalida = interpretar_resposta(
            RESPOSTA_ERRO.replace("E0123", "EM016").replace("CEP do tomador inválido", "Série da DPS inválida").encode()
        )
        self.cliente.gerar_nfse.side_effect = [serie_invalida, self.nota]
        registros = {}
        aceita, relatorio = emissao.testar_series(
            venda_exemplo(), self.cliente,
            lambda s: registros.setdefault(s, RegistroDps(self.lote / f"num_{s}.json", s)),
            emissao.ControleEnvios(self.lote), self.lote, ["900", "1", "2"],
        )
        self.assertEqual(aceita, "1")
        self.assertEqual(len(relatorio), 2)  # parou ao autorizar: a série 2 nunca foi tentada
        series_enviadas = [etree.fromstring(c.args[0]).findtext(".//{*}serie") for c in self.cliente.gerar_nfse.call_args_list]
        self.assertEqual(series_enviadas, ["900", "1"])
        controle = emissao.ControleEnvios(self.lote)
        self.assertEqual((controle.envios["1700001"].situacao, controle.envios["1700001"].serie), (emissao.AUTORIZADA, "1"))

    def test_pendente_consulta_na_serie_do_envio_anterior(self):
        # Envio na série 70002 cai sem resposta; depois a série do lote muda para 900.
        self.cliente.gerar_nfse.side_effect = ErroComunicacao("timeout")
        primeiro = emissao.emitir_venda(venda_exemplo(), self.cliente, self.registro,
                                        emissao.ControleEnvios(self.lote), self.lote, serie="70002")
        self.assertEqual((primeiro.situacao, primeiro.serie), (emissao.PENDENTE, "70002"))
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        outro = RegistroDps(self.lote / "num_900.json", "900")
        envio = emissao.emitir_venda(venda_exemplo(), self.cliente, outro, emissao.ControleEnvios(self.lote), self.lote,
                                     serie="900")
        # A consulta usa a série/número do envio sem resposta, e a nota encontrada evita o reenvio.
        self.cliente.consultar_nfse_por_dps.assert_called_once_with("70002", 1)
        self.assertEqual(envio.situacao, emissao.AUTORIZADA)
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)

    def test_buscar_documentos_salva_pdf_e_links(self):
        self.cliente.gerar_nfse.return_value = self.nota
        self.emitir()
        links = {"visualizacao": "https://iss.exemplo/nfse/1", "autenticidade": "https://iss.exemplo/aut/1"}
        self.cliente.consultar_url_nfse.return_value = (interpretar_resposta(b"<R/>"), links)
        self.cliente.baixar.return_value = (b"%PDF-1.4 conteudo", "application/pdf")
        relatorio = emissao.buscar_documentos(self.cliente, emissao.ControleEnvios(self.lote), self.lote)
        self.assertIn("PDF salvo", relatorio[0])
        self.assertTrue((self.lote / "pdfs" / "NFSe_770636_pedido_1700001.pdf").exists())
        salvos = json.loads((self.lote / "links.json").read_text("utf-8"))
        self.assertEqual(salvos["770636"]["autenticidade"], "https://iss.exemplo/aut/1")

    def test_buscar_documentos_usa_ambiente_nacional_quando_issnet_nega(self):
        self.cliente.gerar_nfse.return_value = self.nota
        self.emitir()
        negado = interpretar_resposta(
            RESPOSTA_ERRO.replace("E0123", "L090").replace("CEP do tomador inválido", "Usuário sem autorização").encode()
        )
        self.cliente.consultar_url_nfse.return_value = (negado, {})
        self.cliente.baixar.return_value = (b"%PDF-1.7 danfse", "application/pdf")
        relatorio = emissao.buscar_documentos(self.cliente, emissao.ControleEnvios(self.lote), self.lote)
        self.assertIn("PDF salvo", relatorio[0])
        chave = "53001081211222333000181000000077063626101792006500"
        self.cliente.baixar.assert_called_once_with(f"https://adn.nfse.gov.br/danfse/{chave}")
        salvos = json.loads((self.lote / "links.json").read_text("utf-8"))
        self.assertIn(chave, salvos["770636"]["consulta_publica"])

    def test_buscar_documentos_quando_link_nao_e_pdf(self):
        self.cliente.gerar_nfse.return_value = self.nota
        self.emitir()
        self.cliente.consultar_url_nfse.return_value = (interpretar_resposta(b"<R/>"), {"visualizacao": "https://x/1"})
        self.cliente.baixar.return_value = (b"<html>pagina</html>", "text/html")
        relatorio = emissao.buscar_documentos(self.cliente, emissao.ControleEnvios(self.lote), self.lote)
        self.assertIn("PDF não baixado", relatorio[0])
        self.assertEqual(list((self.lote / "pdfs").glob("*.pdf")), [])

    def test_diagnostico_para_no_primeiro_perfil_aceito(self):
        erro_assinatura = interpretar_resposta(
            RESPOSTA_ERRO.replace("E0123", "E0714").replace("CEP do tomador inválido", "Erro na assinatura").encode()
        )
        self.cliente.gerar_nfse.side_effect = [erro_assinatura, erro_assinatura, self.nota]
        relatorio = emissao.diagnosticar_assinatura(
            venda_exemplo(), self.cliente, self.registro, emissao.ControleEnvios(self.lote), self.lote
        )
        self.assertEqual(len(relatorio), 3)
        self.cliente.salvar_perfil.assert_called_once_with(PERFIS[2])
        chamadas = self.cliente.gerar_nfse.call_args_list
        self.assertEqual([c.args[1] for c in chamadas], list(PERFIS[:3]))
        # A cada perfil a DPS é reassinada, sempre com o mesmo número: nada de DPS nova por tentativa.
        self.assertEqual({etree.fromstring(c.args[0]).findtext(".//{*}nDPS") for c in chamadas}, {"1"})

    def test_erro_interno_do_issnet_fica_pendente_e_consulta_antes_de_reenviar(self):
        erro_interno = interpretar_resposta(
            RESPOSTA_ERRO.replace("E0123", "E232").replace("CEP do tomador inválido",
                                                          "Ocorreu um erro no processamento do arquivo").encode()
        )
        self.cliente.gerar_nfse.return_value = erro_interno
        self.assertEqual(self.emitir().situacao, emissao.PENDENTE)
        # A nota tinha sido gerada apesar do erro: a consulta acha e não reenvia.
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        envio = self.emitir()
        self.assertEqual(envio.situacao, emissao.AUTORIZADA)
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)

    def test_rejeitada_antiga_por_e232_tambem_consulta_antes(self):
        controle = emissao.ControleEnvios(self.lote)
        numero = self.registro.numeros_para([venda_exemplo().pedido])[venda_exemplo().pedido]
        controle.registrar(emissao.Envio(venda_exemplo().pedido, numero, emissao.REJEITADA, serie="70002",
                                         mensagens=["E232 - Ocorreu um erro no processamento do arquivo."]))
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        envio = self.emitir()
        self.assertEqual(envio.situacao, emissao.AUTORIZADA)
        self.cliente.gerar_nfse.assert_not_called()

    def dps_ja_existe(self):
        self.cliente.gerar_nfse.return_value = interpretar_resposta(
            RESPOSTA_ERRO.replace("E0123", "E0014").replace("CEP do tomador inválido", "DPS já existe numa NFS-e").encode())

    def test_e0014_com_a_nota_do_proprio_pedido_vira_autorizada_sem_reenviar(self):
        self.dps_ja_existe()
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        envio = self.emitir()
        self.assertEqual((envio.situacao, envio.numero_nfse), (emissao.AUTORIZADA, "770636"))
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)

    def test_e0014_com_nota_de_outro_pedido_descarta_o_numero(self):
        self.dps_ja_existe()
        self.nota.notas[0].documento_tomador = "11111111000191"
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        pedido = venda_exemplo().pedido
        envio = self.emitir()
        self.assertEqual(envio.situacao, emissao.REJEITADA)
        self.assertIn("é de outro pedido", envio.mensagens[0])
        self.assertEqual(self.registro.ja_numerados([pedido]), [])  # o pedido ficou sem número
        self.assertIn(1, self.registro.numeros_usados())  # e o número descartado nunca mais é oferecido
        self.assertEqual(self.registro.reservar([1, 2], [pedido]), {pedido: 2})
        # com outro número reservado, o próximo clique reenvia de verdade (não consulta de novo)
        self.cliente.gerar_nfse.return_value = self.nota
        self.assertEqual(self.emitir().situacao, emissao.AUTORIZADA)
        self.assertEqual(self.cliente.gerar_nfse.call_count, 2)
        self.assertEqual(self.cliente.consultar_nfse_por_dps.call_count, 1)

    def test_e0014_sem_boleto_para_conferir_nao_decide_sozinho(self):
        self.dps_ja_existe()
        self.nota.notas[0].descricao = "Ressarcimento sem número de boleto"
        self.cliente.consultar_nfse_por_dps.return_value = self.nota
        pedido = venda_exemplo().pedido
        envio = self.emitir()
        self.assertEqual(envio.situacao, emissao.REJEITADA)
        self.assertIn("Confira no ISS Net", envio.mensagens[0])
        self.assertEqual(self.registro.ja_numerados([pedido]), [pedido])  # nada foi descartado
        self.emitir()  # clicar de novo só consulta, não reenvia a mesma DPS
        self.assertEqual(self.cliente.gerar_nfse.call_count, 1)

    def test_rejeitada_guarda_motivo_e_mesmo_numero(self):
        self.cliente.gerar_nfse.return_value = interpretar_resposta(RESPOSTA_ERRO.encode())
        primeiro = self.emitir()
        self.assertEqual(primeiro.situacao, emissao.REJEITADA)
        self.assertIn("CEP do tomador inválido", primeiro.mensagens[0])
        self.cliente.gerar_nfse.return_value = self.nota
        segundo = self.emitir()
        self.assertEqual(segundo.numero_dps, primeiro.numero_dps)


if __name__ == "__main__":
    unittest.main()
