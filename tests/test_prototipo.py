"""Testes com os arquivos reais de 16/09/2026 e as NFS-e baixadas do ISS Net.

Rodar na pasta emissor_nfse:  python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nfse.conferencia import associar, conferir  # noqa: E402
from nfse.documentos import cnpj_valido, cpf_valido  # noqa: E402
from nfse.dps import FUSO_BRASILIA, composicao_valor, descricao_servico, montar_dps  # noqa: E402
from nfse.nfse_xml import ler_nfse, remover_namespaces  # noqa: E402
from nfse.numeracao import RegistroDps  # noqa: E402
from nfse.remessa import Cliente, Venda, ler_remessa  # noqa: E402
from nfse.retorno import RetornoNaoConfigurado, chave_para_gerencia, gerar_retorno, ler_retorno  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def nota(numero: str):
    return ler_nfse((FIX / f"nfse_{numero}.xml").read_bytes())[0]


def venda_exemplo() -> Venda:
    """Reproduz a venda da NFS-e 770636 (14/10/2026), para comparar a nossa DPS com a real."""
    cliente = Cliente(
        documento="10001053000180", inscricao_estadual="", razao_social="CLIENTE EXEMPLO LTDA",
        nome_fantasia="", logradouro="RUA EXEMPLO", numero="74", complemento="COMPL",
        cod_municipio="1600501", uf="AP", cep="68980000", bairro="PARAISO", linha=1,
    )
    return Venda(
        pedido="1700001", cliente=cliente, linha=2, cod_servico="13",
        descricao="Ressarcimento de custos de utilizacao de recursos de tecnologia da informacao para "
        "fornecimento de plataforma para a realizacao de licitacoes.",
        detalhe="Periodo de Avulso de uso do Portal Exemplo, boleto IU1612217.",
        quantidade=Decimal("1.000"), valor_unitario=Decimal("129.0000"), tem_item=True,
    )


def caminhos(elemento, prefixo=""):
    atual = f"{prefixo}/{elemento.tag}"
    saida = [atual]
    for filho in elemento:
        saida += caminhos(filho, atual)
    return saida


def folhas(elemento, prefixo=""):
    atual = f"{prefixo}/{elemento.tag}"
    if len(elemento) == 0:
        return {atual: elemento.text}
    saida = {}
    for filho in elemento:
        saida.update(folhas(filho, atual))
    return saida


class TestDocumentos(unittest.TestCase):
    def test_cnpj(self):
        self.assertTrue(cnpj_valido("11.222.333/0001-81"))
        self.assertFalse(cnpj_valido("11222333000182"))
        self.assertFalse(cnpj_valido("00000000000000"))

    def test_cnpj_alfanumerico(self):
        self.assertTrue(cnpj_valido("12.ABC.345/01DE-35"))

    def test_cpf(self):
        self.assertTrue(cpf_valido("529.982.247-25"))
        self.assertFalse(cpf_valido("11111111111"))
        self.assertFalse(cpf_valido("52998224724"))


class TestRemessa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remessa = ler_remessa((FIX / "remessa_16092026.txt").read_bytes(), "NFE_202609160838.txt")

    def test_totais(self):
        self.assertEqual(self.remessa.erros_arquivo, [])
        self.assertEqual(len(self.remessa.vendas), 1051)
        self.assertEqual(self.remessa.valor_total, Decimal("217785.00"))

    def test_pacotes(self):
        pacotes = Counter(v.pacote for v in self.remessa.vendas)
        self.assertEqual(pacotes, {"mensal": 952, "avulso": 65, "anual": 30, "semestral": 4})

    def test_cliente_sem_documento_fica_bloqueado(self):
        for pedido in ("1666183", "1665822"):
            venda = self.remessa.venda(pedido)
            self.assertFalse(venda.pronta)
            self.assertIn("Cliente sem CPF/CNPJ", venda.erros)

    def test_cliente_com_duas_vendas_gera_aviso(self):
        venda = self.remessa.venda("1675245")
        self.assertEqual(venda.cliente.documento, "10000997000133")
        self.assertTrue(any("2 vendas" in a for a in venda.avisos))

    def test_venda_normal(self):
        venda = self.remessa.venda("1675227")
        self.assertTrue(venda.pronta, venda.erros)
        self.assertEqual(venda.valor_total, Decimal("165.00"))
        self.assertEqual(venda.boleto, "IU1580472")
        self.assertEqual(venda.cliente.numero, "160")
        self.assertEqual(venda.cliente.logradouro, "Rua Exemplo 996")

    def test_extrair_vendas_mantem_linhas_originais(self):
        from nfse.remessa import extrair_vendas
        original = (FIX / "remessa_16092026.txt").read_bytes()
        parcial = ler_remessa(extrair_vendas(original, ["1675227", "1675245", "1675272"]))
        self.assertEqual(sorted(v.pedido for v in parcial.vendas), ["1675227", "1675245", "1675272"])
        # o cliente tem 2 vendas: um único registro C seguido dos dois pares V/I.
        self.assertIs(parcial.venda("1675245").cliente, parcial.venda("1675272").cliente)
        self.assertEqual(parcial.venda("1675227").cliente.logradouro, "Rua Exemplo 996")

    def test_campos_so_com_pontos_sao_descartados(self):
        cliente = self.remessa.venda("1675826").cliente  # cliente com fantasia "....." e complemento "..."
        self.assertEqual(cliente.nome_fantasia, "")
        self.assertEqual(cliente.complemento, "")


class TestDps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real = remover_namespaces(ET.parse(FIX / "nfse_770636.xml").getroot())
        cls.real = real.find(".//DPS/infDPS")
        emitida = datetime(2026, 10, 14, 16, 35, 7, tzinfo=FUSO_BRASILIA)
        xml = montar_dps(venda_exemplo(), "70001", 770636, emitida)
        cls.nossa = remover_namespaces(ET.fromstring(xml)).find("infDPS")

    # A DPS real (770636) veio do emissor web do ISS Net. Pelo web service o ISS Net exige o código
    # municipal da empresa (105) e proíbe pAliq para não optante com ISS no DF (produção, 19/10/2026).
    DIFERENCAS_WEB_SERVICE = {"pAliq"}

    def test_mesma_estrutura_da_dps_real(self):
        def sem_diferencas(arvore):
            return [c for c in caminhos(arvore) if c.rsplit("/", 1)[1] not in self.DIFERENCAS_WEB_SERVICE]
        self.assertEqual(caminhos(self.nossa), sem_diferencas(self.real))
        self.assertIsNone(self.nossa.find(".//pAliq"))

    def test_mesmo_id_da_dps_real(self):
        self.assertEqual(self.nossa.get("Id"), self.real.get("Id"))

    def test_mesmos_valores_da_dps_real(self):
        # tpAmb: protótipo em homologação. verAplic: nosso sistema. Descrição e informações
        # complementares: corrigimos a formatação do emissor anterior e o ISS Net acrescenta PROCON sozinho.
        ignorar = {"tpAmb", "verAplic", "xDescServ", "xInfComp", "cTribMun", *self.DIFERENCAS_WEB_SERVICE}
        reais = {k: v for k, v in folhas(self.real).items() if k.rsplit("/", 1)[1] not in ignorar}
        nossos = {k: v for k, v in folhas(self.nossa).items() if k.rsplit("/", 1)[1] not in ignorar}
        self.assertEqual(nossos, reais)
        self.assertEqual(self.nossa.findtext(".//cTribMun"), "105")

    def test_composicao_do_valor_igual_aos_textos_de_referencia(self):
        # Textos enviados pela usuária em 21/10/2026 (anual, semestral de R$ 665, mensal, avulso).
        referencias = {
            "1650": "Valor bruto faturado: R$ 1.650,00, composto pelo preço líquido de R$ 1.556,78, acrescido dos "
                    "tributos incidentes sobre o faturamento: PIS (0,65%): R$ 10,73, COFINS (3%): R$ 49,50, "
                    "ISS (2%): R$ 33,00. Tributos calculados sobre o valor bruto da operação.",
            "665": "Valor bruto faturado: R$ 665,00, composto pelo preço líquido de R$ 627,43, acrescido dos "
                   "tributos incidentes sobre o faturamento: PIS (0,65%): R$ 4,32, COFINS (3%): R$ 19,95, "
                   "ISS (2%): R$ 13,30. Tributos calculados sobre o valor bruto da operação.",
            "165": "Valor bruto faturado: R$ 165,00, composto pelo preço líquido de R$ 155,68, acrescido dos "
                   "tributos incidentes sobre o faturamento: PIS (0,65%): R$ 1,07, COFINS (3%): R$ 4,95, "
                   "ISS (2%): R$ 3,30. Tributos calculados sobre o valor bruto da operação.",
            "129": "Valor bruto faturado: R$ 129,00, composto pelo preço líquido de R$ 121,71, acrescido dos "
                   "tributos incidentes sobre o faturamento: PIS (0,65%): R$ 0,84, COFINS (3%): R$ 3,87, "
                   "ISS (2%): R$ 2,58. Tributos calculados sobre o valor bruto da operação.",
        }
        for valor, texto in referencias.items():
            self.assertEqual(composicao_valor(Decimal(valor + ".00")), texto, valor)

    def test_informacoes_complementares_na_dps(self):
        texto = self.nossa.findtext(".//xInfComp")
        self.assertTrue(texto.startswith("Conforme SC Cosit n° 157/2023 e 243/2017, ficam dispensadas"))
        self.assertTrue(texto.endswith("ISS (2%): R$ 2,58. Tributos calculados sobre o valor bruto da operação."))
        self.assertIn("serviço de programação. Valor bruto faturado: R$ 129,00", texto)

    def test_descricao_sem_lixo_da_remessa(self):
        texto = descricao_servico(venda_exemplo())
        self.assertNotIn(";", texto)
        self.assertTrue(texto.endswith("boleto IU1612217. - Quantidade: 1,0000 (X) Valor unitário: 129,00 (=) Total: R$ 129,00"))


class TestTextoAceito(unittest.TestCase):
    def test_travessao_aspas_curvas_e_fora_do_latin1_viram_caracteres_aceitos(self):
        venda = venda_exemplo()
        venda.cliente.logradouro = "RODOVIA GOVERNADOR MARIO COVAS 3979 – KM 268 “A” Ŝão João"
        dps = remover_namespaces(ET.fromstring(montar_dps(venda, "3", 1))).find("infDPS")
        self.assertEqual(dps.findtext(".//xLgr"), 'RODOVIA GOVERNADOR MARIO COVAS 3979 - KM 268 "A" São João')
        # Latin-1 (acentos do português) fica como está.
        self.assertEqual(dps.findtext(".//xNome"), venda.cliente.razao_social)


class TestNotas(unittest.TestCase):
    def test_nota_nacional(self):
        n = nota("770636")
        self.assertEqual((n.formato, n.numero, n.status), ("nacional", "770636", "100"))
        self.assertEqual(n.chave, "53001081211222333000181000000077063626101792006500")
        self.assertEqual(len(n.chave), 50)
        self.assertEqual((n.serie_dps, n.numero_dps), ("70001", "770636"))
        self.assertEqual(n.valor_servico, Decimal("129.00"))
        self.assertEqual(n.valor_iss, Decimal("2.58"))
        self.assertFalse(n.iss_retido)
        self.assertEqual(n.boleto, "IU1612217")

    def test_nota_abrasf(self):
        n = nota("761522")
        self.assertEqual((n.formato, n.numero, n.chave), ("abrasf", "761522", ""))
        self.assertEqual(n.documento_tomador, "10000995000144")
        self.assertEqual(n.boleto, "IU1580472")


class TestConferencia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.remessa = ler_remessa((FIX / "remessa_16092026.txt").read_bytes())
        retorno_anterior = ler_retorno((FIX / "retorno_anterior_16092026.txt").read_bytes())
        cls.pedido_anterior = {r["NR_NOTA"]: pedido for pedido, r in retorno_anterior.items()}

    def test_notas_corretas_passam(self):
        for numero, pedido in (("761522", "1675227"), ("761538", "1675104")):
            self.assertEqual(self.pedido_anterior[numero], pedido)
            self.assertEqual(conferir(self.remessa.venda(pedido), nota(numero)), [])

    def test_detecta_nota_trocada_no_retorno_da_anterior(self):
        # A emissor anterior ligou a nota 761520 (R$ 165) ao pedido 1675245 (R$ 129).
        pedido = self.pedido_anterior["761520"]
        self.assertEqual(pedido, "1675245")
        divergencias = conferir(self.remessa.venda(pedido), nota("761520"))
        self.assertTrue(any(d.startswith("Valor da nota") for d in divergencias), divergencias)

    def test_associa_ao_pedido_certo(self):
        with tempfile.TemporaryDirectory() as pasta:
            registro = RegistroDps(Path(pasta) / "num.json", "70002")
            resultados = associar(self.remessa, [nota("761520"), nota("761522")], registro)
        trocada, correta = resultados
        self.assertEqual(trocada.venda.pedido, "1675272")  # o pedido de R$ 165 desse cliente
        self.assertFalse(trocada.apta)  # descrição truncada sem boleto + formato antigo
        self.assertEqual(correta.venda.pedido, "1675227")


class TestRetorno(unittest.TestCase):
    def test_layout_igual_ao_da_anterior(self):
        gerado = datetime(2026, 10, 16, 14, 25, 49, tzinfo=FUSO_BRASILIA)
        with mock.patch("config.URL_BASE_ARQUIVOS", "https://arquivos.exemplo/nfse"), \
                mock.patch("config.MODO_URLS_RETORNO", "hospedagem"):
            nome, conteudo = gerar_retorno([("1700001", nota("770636"))], gerado)
        self.assertEqual(nome, "00000nota_16102026142549.txt")
        texto = conteudo.decode("utf-8")
        self.assertNotIn("\r", texto)
        self.assertTrue(texto.endswith("\n"))
        campos = texto.strip().split(";")
        self.assertEqual(len(campos), 6)
        # TX_CHAVE no formato curto do emissor anterior (cabe nos 44 do Gerência); a chave de 50 vai nas URLs.
        self.assertEqual(campos[:4], ["1700001", "770636", "100", "11222333000181_1700001_770636_70001_770636"])
        self.assertLessEqual(len(campos[3]), 44)
        chave = nota("770636").chave
        self.assertTrue(campos[4].endswith(f"{chave}-nfse.xml"))
        self.assertTrue(campos[5].endswith(f"{chave}.pdf"))

    def test_chave_no_formato_da_anterior(self):
        n = nota("770636")
        n.serie_dps, n.numero_dps = "3", "511"
        self.assertEqual(chave_para_gerencia("1694209", n), "11222333000181_1694209_511_003_770636")

    def test_retorno_bloqueado_sem_endereco_dos_arquivos(self):
        with mock.patch("config.MODO_URLS_RETORNO", "hospedagem"), \
                mock.patch("config.URL_BASE_ARQUIVOS", "https://A-DEFINIR/nfse"), \
                self.assertRaises(RetornoNaoConfigurado):
            gerar_retorno([("1700001", nota("770636"))], datetime.now(FUSO_BRASILIA))

    def test_retorno_com_consulta_publica(self):
        with mock.patch("config.MODO_URLS_RETORNO", "consulta_publica"):
            _, conteudo = gerar_retorno([("1700001", nota("770636"))], datetime.now(FUSO_BRASILIA))
        campos = conteudo.decode().strip().split(";")
        self.assertEqual(campos[4], f"https://www.nfse.gov.br/ConsultaPublica/?tpc=1&chave={nota('770636').chave}")
        self.assertEqual(campos[4], campos[5])

    def test_so_entram_notas_autorizadas(self):
        rejeitada = nota("770636")
        rejeitada.status = "204"
        _, conteudo = gerar_retorno([("1", rejeitada)], datetime.now(FUSO_BRASILIA))
        self.assertEqual(conteudo, b"")


class TestNumeracao(unittest.TestCase):
    def test_pedido_nunca_recebe_dois_numeros(self):
        with tempfile.TemporaryDirectory() as pasta:
            registro = RegistroDps(Path(pasta) / "num.json", "70002")
            self.assertEqual(registro.numeros_para(["A", "B"]), {"A": 1, "B": 2})
            self.assertEqual(registro.numeros_para(["B", "C"]), {"B": 2, "C": 3})
            self.assertEqual(registro.pedido_do_numero(3), "C")
            with self.assertRaises(ValueError):
                RegistroDps(Path(pasta) / "num.json", "70001").numeros_para(["D"])

    def test_reserva_numeros_disponiveis_do_issnet(self):
        with tempfile.TemporaryDirectory() as pasta:
            registro = RegistroDps(Path(pasta) / "num3.json", "3")
            registro.numeros_para(["A"])  # A já tinha o número 1
            novas = registro.reservar([103, 52, 1, 205], ["A", "B", "C", "D", "E"])
            # A mantém o 1; o 1 não é dado a ninguém; B, C, D recebem 52, 103, 205; E fica sem número.
            self.assertEqual(novas, {"B": 52, "C": 103, "D": 205})
            self.assertEqual(registro.numeros_para(["A", "B"]), {"A": 1, "B": 52})
            self.assertEqual(registro.reservar([52, 103], ["B", "C"]), {})  # repetir não muda nada


if __name__ == "__main__":
    unittest.main()
