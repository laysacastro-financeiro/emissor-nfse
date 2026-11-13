"""Usuários, login e auditoria do emissor (uso compartilhado pela equipe na rede).

- Senhas guardadas só como hash (werkzeug); nunca em texto.
- O primeiro acesso cria o administrador; ele cadastra e desativa os demais usuários.
- Todo POST exige o token anti-CSRF da sessão (impede que outro site dispare ações pelo navegador).
- Ações importantes vão para dados/auditoria.log com o usuário que as fez.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from functools import wraps

from flask import abort, flash, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import config
import segredos
from nfse.dps import FUSO_BRASILIA

ARQUIVO_USUARIOS = config.DADOS_DIR / "usuarios.json"
ARQUIVO_AUDITORIA = config.DADOS_DIR / "auditoria.log"
RE_LOGIN = re.compile(r"^[a-z0-9._-]{3,30}$")
TAMANHO_MINIMO_SENHA = 8
TAMANHO_MAXIMO_SENHA = 128  # senha gigante só serve para sobrecarregar o servidor
ROTAS_LIVRES = {"entrar", "primeiro_acesso", "static"}
TENTATIVAS_LOGIN = 5  # erros de senha por login (ou 4x isso por endereço) antes de bloquear
JANELA_BLOQUEIO_LOGIN = 15 * 60  # segundos
_trava = threading.Lock()
_falhas_login: dict[str, list[float]] = {}
# Hash de uma senha qualquer: login inexistente gasta o mesmo tempo que login existente (não revela quem é usuário).
_HASH_FALSO = generate_password_hash(secrets.token_hex(8))


def segredo_sessao() -> str:
    """Segredo que assina os cookies de sessão: variável de ambiente ou um aleatório guardado em dados/."""
    return segredos.segredo("segredo_sessao", "EMISSOR_SEGREDO_SESSAO")


def _falhas_recentes(chave: str) -> list[float]:
    limite = time.monotonic() - JANELA_BLOQUEIO_LOGIN
    recentes = [t for t in _falhas_login.get(chave, []) if t > limite]
    _falhas_login[chave] = recentes
    return recentes


def login_bloqueado(login: str, endereco: str) -> bool:
    """Muitas senhas erradas seguidas: bloqueia o login (e o endereço) por 15 minutos."""
    with _trava:
        return (len(_falhas_recentes(f"login:{login}")) >= TENTATIVAS_LOGIN
                or len(_falhas_recentes(f"ip:{endereco}")) >= TENTATIVAS_LOGIN * 4)


def registrar_falha_login(login: str, endereco: str) -> None:
    with _trava:
        for chave in (f"login:{login}", f"ip:{endereco}"):
            _falhas_recentes(chave).append(time.monotonic())


def limpar_falhas_login(login: str) -> None:
    with _trava:
        _falhas_login.pop(f"login:{login}", None)


def carregar_usuarios() -> dict:
    return json.loads(ARQUIVO_USUARIOS.read_text("utf-8")) if ARQUIVO_USUARIOS.exists() else {}


def _gravar_usuarios(usuarios: dict) -> None:
    ARQUIVO_USUARIOS.parent.mkdir(parents=True, exist_ok=True)
    temporario = ARQUIVO_USUARIOS.with_suffix(".tmp")
    temporario.write_text(json.dumps(usuarios, ensure_ascii=False, indent=1), "utf-8")
    temporario.replace(ARQUIVO_USUARIOS)


def validar_novo_usuario(login: str, nome: str, senha: str, confirmacao: str) -> str | None:
    if not RE_LOGIN.match(login):
        return "Login deve ter de 3 a 30 caracteres: letras minúsculas, números, ponto, hífen ou sublinhado."
    if not nome.strip():
        return "Informe o nome."
    if len(senha) < TAMANHO_MINIMO_SENHA:
        return f"A senha deve ter pelo menos {TAMANHO_MINIMO_SENHA} caracteres."
    if len(senha) > TAMANHO_MAXIMO_SENHA:
        return f"A senha pode ter no máximo {TAMANHO_MAXIMO_SENHA} caracteres."
    if senha != confirmacao:
        return "A confirmação não confere com a senha."
    return None


def criar_usuario(login: str, nome: str, senha: str, admin: bool, criado_por: str) -> str | None:
    with _trava:
        usuarios = carregar_usuarios()
        if login in usuarios:
            return "Já existe um usuário com esse login."
        usuarios[login] = {
            "nome": nome.strip(),
            "senha_hash": generate_password_hash(senha),
            "admin": admin,
            "ativo": True,
            "criado_em": datetime.now(FUSO_BRASILIA).isoformat(timespec="seconds"),
            "criado_por": criado_por,
        }
        _gravar_usuarios(usuarios)
    return None


def alterar_usuario(login: str, **campos) -> None:
    with _trava:
        usuarios = carregar_usuarios()
        if login not in usuarios:
            return
        if "senha" in campos:
            usuarios[login]["senha_hash"] = generate_password_hash(campos.pop("senha"))
        usuarios[login].update(campos)
        _gravar_usuarios(usuarios)


def conferir_senha(login: str, senha: str) -> dict | None:
    usuario = carregar_usuarios().get(login)
    senha = senha[:TAMANHO_MAXIMO_SENHA + 1]  # nunca calcula hash de texto gigante
    if usuario is None:
        check_password_hash(_HASH_FALSO, senha)  # mesmo tempo de resposta de um login que existe
        return None
    if usuario.get("ativo") and check_password_hash(usuario["senha_hash"], senha):
        return usuario
    return None


def usuario_logado() -> dict | None:
    login = session.get("usuario")
    usuario = carregar_usuarios().get(login) if login else None
    if usuario and usuario.get("ativo"):
        return {"login": login, **usuario}
    return None


def token_csrf() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def auditar(acao: str) -> None:
    usuario = session.get("usuario", "-")
    ARQUIVO_AUDITORIA.parent.mkdir(parents=True, exist_ok=True)
    with _trava, open(ARQUIVO_AUDITORIA, "a", encoding="utf-8") as log:
        log.write(f"{datetime.now(FUSO_BRASILIA):%d/%m/%Y %H:%M:%S} | {usuario} | {request.remote_addr} | {acao}\n")


def proteger(app) -> None:
    """Exige login em todas as páginas e token anti-CSRF em todo POST."""

    @app.before_request
    def _exigir_login():
        if request.endpoint in ROTAS_LIVRES:
            if request.method == "POST":
                _conferir_csrf()
            return None
        if not carregar_usuarios():
            return redirect(url_for("primeiro_acesso"))
        if usuario_logado() is None:
            return redirect(url_for("entrar", proxima=request.full_path if request.method == "GET" else "/"))
        if request.method == "POST":
            _conferir_csrf()
        return None

    @app.context_processor
    def _dados_usuario():
        return {"usuario": usuario_logado(), "csrf_token": token_csrf()}


def _conferir_csrf() -> None:
    enviado = request.form.get("csrf_token", "")
    if not enviado or not secrets.compare_digest(enviado, session.get("csrf", "")):
        abort(400, "Formulário expirado. Recarregue a página (F5) e tente de novo.")


def somente_admin(funcao):
    @wraps(funcao)
    def interna(*args, **kwargs):
        usuario = usuario_logado()
        if not usuario or not usuario.get("admin"):
            flash("Apenas administradores acessam essa página.", "erro")
            return redirect(url_for("inicio"))
        return funcao(*args, **kwargs)
    return interna


def destino_seguro(proxima: str) -> str:
    """Só redireciona para caminhos internos (evita mandar o usuário para outro site após o login)."""
    return proxima if proxima.startswith("/") and not proxima.startswith("//") else "/"


def registrar_rotas(app) -> None:
    from flask import render_template

    @app.route("/primeiro-acesso", methods=["GET", "POST"])
    def primeiro_acesso():
        if carregar_usuarios():
            return redirect(url_for("entrar"))
        # Quem cria o primeiro usuário vira administrador: só deixa isso a partir do próprio computador, para um
        # sistema recém-instalado e já exposto na rede não ser tomado por outra pessoa.
        if request.remote_addr not in ("127.0.0.1", "::1") and not os.environ.get("EMISSOR_PRIMEIRO_ACESSO_REMOTO"):
            abort(403, "O primeiro acesso (criação do administrador) só pode ser feito no computador onde o sistema roda.")
        if request.method == "POST":
            login = request.form.get("login", "").strip().lower()
            erro = validar_novo_usuario(login, request.form.get("nome", ""), request.form.get("senha", ""),
                                        request.form.get("confirmacao", ""))
            if erro is None:
                erro = criar_usuario(login, request.form["nome"], request.form["senha"], True, "primeiro acesso")
            if erro:
                flash(erro, "erro")
            else:
                session.clear()
                session["usuario"] = login
                session.permanent = True
                auditar("criou o primeiro usuário (administrador)")
                flash("Administrador criado. Cadastre a equipe em Usuários.", "ok")
                return redirect(url_for("inicio"))
        return render_template("primeiro_acesso.html")

    @app.route("/entrar", methods=["GET", "POST"])
    def entrar():
        if not carregar_usuarios():
            return redirect(url_for("primeiro_acesso"))
        if request.method == "POST":
            login = request.form.get("login", "").strip().lower()[:60]
            if login_bloqueado(login, request.remote_addr or "-"):
                auditar(f"login bloqueado por excesso de tentativas ({login})")
                flash("Muitas tentativas de entrada. Aguarde 15 minutos e tente de novo.", "erro")
            elif conferir_senha(login, request.form.get("senha", "")):
                limpar_falhas_login(login)
                csrf = session.get("csrf")
                session.clear()
                session["usuario"] = login
                session.permanent = True  # expira após PERMANENT_SESSION_LIFETIME (app.py)
                if csrf:
                    session["csrf"] = csrf
                auditar("entrou no sistema")
                return redirect(destino_seguro(request.form.get("proxima", "/")))
            else:
                registrar_falha_login(login, request.remote_addr or "-")
                flash("Login ou senha inválidos.", "erro")
        return render_template("entrar.html", proxima=request.args.get("proxima", "/"))

    @app.post("/sair")
    def sair():
        auditar("saiu do sistema")
        session.clear()
        return redirect(url_for("entrar"))

    @app.route("/usuarios", methods=["GET", "POST"])
    @somente_admin
    def usuarios():
        if request.method == "POST":
            acao = request.form.get("acao")
            login = request.form.get("login", "").strip().lower()
            if acao == "criar":
                erro = validar_novo_usuario(login, request.form.get("nome", ""), request.form.get("senha", ""),
                                            request.form.get("confirmacao", ""))
                if erro is None:
                    erro = criar_usuario(login, request.form["nome"], request.form["senha"],
                                         request.form.get("admin") == "sim", session["usuario"])
                if erro:
                    flash(erro, "erro")
                else:
                    auditar(f"cadastrou o usuário {login}")
                    flash(f"Usuário {login} cadastrado.", "ok")
            elif acao in ("desativar", "reativar") and login != session["usuario"]:
                alterar_usuario(login, ativo=acao == "reativar")
                auditar(f"{acao} o usuário {login}")
                flash(f"Usuário {login}: {acao.replace('desativar', 'desativado').replace('reativar', 'reativado')}.", "ok")
            elif acao == "nova_senha":
                senha = request.form.get("senha", "")
                erro = validar_novo_usuario(login, "x", senha, request.form.get("confirmacao", ""))
                if erro:
                    flash(erro, "erro")
                else:
                    alterar_usuario(login, senha=senha)
                    auditar(f"definiu nova senha para {login}")
                    flash(f"Senha de {login} alterada.", "ok")
            return redirect(url_for("usuarios"))
        return render_template("usuarios.html", lista=sorted(carregar_usuarios().items()))

    @app.route("/minha-senha", methods=["GET", "POST"])
    def minha_senha():
        if request.method == "POST":
            login = session["usuario"]
            if not conferir_senha(login, request.form.get("atual", "")):
                flash("Senha atual incorreta.", "erro")
            else:
                erro = validar_novo_usuario(login, "x", request.form.get("senha", ""), request.form.get("confirmacao", ""))
                if erro:
                    flash(erro, "erro")
                else:
                    alterar_usuario(login, senha=request.form["senha"])
                    auditar("trocou a própria senha")
                    flash("Senha alterada.", "ok")
                    return redirect(url_for("inicio"))
        return render_template("minha_senha.html")


def ultimas_auditorias(quantidade: int = 200) -> list[str]:
    if not ARQUIVO_AUDITORIA.exists():
        return []
    return ARQUIVO_AUDITORIA.read_text("utf-8").splitlines()[-quantidade:][::-1]
