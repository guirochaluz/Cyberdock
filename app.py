import os
import warnings

# 1) Suprime todos os DeprecationWarning do Python
os.environ["PYTHONWARNINGS"] = "ignore::DeprecationWarning"
warnings.filterwarnings("ignore", category=DeprecationWarning)

# 2) (Opcional) Suprime warnings internos do Streamlit
import logging
logging.getLogger("streamlit").setLevel(logging.ERROR)


from dotenv import load_dotenv
import locale

# 1) Carrega .env antes de tudo
load_dotenv()
COOKIE_SECRET = os.getenv("COOKIE_SECRET")
BACKEND_URL    = os.getenv("BACKEND_URL")
FRONTEND_URL   = os.getenv("FRONTEND_URL")
DB_URL         = os.getenv("DB_URL")
ML_CLIENT_ID   = os.getenv("ML_CLIENT_ID")

# 2) Agora sim importe o Streamlit e configure a página _antes_ de qualquer outra chamada st.*
import streamlit as st
st.set_page_config(
    page_title="Cyber Dock",
    page_icon="favicon.png",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# 3) Depois de set_page_config, importe tudo o mais que precisar
from sales import sync_all_accounts, get_full_sales, revisar_banco_de_dados, get_incremental_sales, traduzir_status
from streamlit_cookies_manager import EncryptedCookieManager
import pandas as pd
import plotly.express as px
import requests
from sqlalchemy import create_engine, text
from streamlit_option_menu import option_menu
from typing import Optional
from wordcloud import WordCloud
import altair as alt
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from textblob import TextBlob
import io
from datetime import datetime, timedelta
from utils import engine, DATA_INICIO, buscar_ml_fee
import time
from reconcile import reconciliar_vendas
from dateutil.relativedelta import relativedelta






# 4) Configuração de locale
try:
    locale.setlocale(locale.LC_ALL, 'pt_BR.UTF-8')
    LOCALE_OK = True
except locale.Error:
    LOCALE_OK = False

def format_currency(valor: float) -> str:
    # ...
    ...

# 5) Validações iniciais de ambiente
if not COOKIE_SECRET:
    st.error("⚠️ Defina COOKIE_SECRET no seu .env")
    st.stop()

if not all([BACKEND_URL, FRONTEND_URL, DB_URL, ML_CLIENT_ID]):
    st.error("❌ Defina BACKEND_URL, FRONTEND_URL, DB_URL e ML_CLIENT_ID em seu .env")
    st.stop()

# 6) Gerenciador de cookies e autenticação
cookies = EncryptedCookieManager(prefix="nexus/", password=COOKIE_SECRET)
if not cookies.ready():
    st.stop()

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if cookies.get("access_token"):
    st.session_state["authenticated"] = True
    st.session_state["access_token"] = cookies["access_token"]

# ----------------- CSS Customizado -----------------
st.markdown("""
<style>
  html, body, [data-testid="stAppViewContainer"] {
    overflow: hidden !important;
    height: 100vh !important;
  }
  ::-webkit-scrollbar { display: none; }
  [data-testid="stSidebar"] {
    background-color: #161b22;
    overflow: hidden !important;
    height: 100vh !important;
  }
  [data-testid="stAppViewContainer"] {
    background-color: #0e1117;
    color: #fff;
  }
  .sidebar-title {
    font-size: 18px;
    font-weight: bold;
    color: #ffffff;
    margin-bottom: 10px;
  }
  .menu-button {
    width: 100%;
    padding: 8px;
    margin-bottom: 5px;
    background-color: #1d2b36;
    color: #fff;
    border: none;
    border-radius: 5px;
    text-align: left;
    cursor: pointer;
  }
  .menu-button:hover {
    background-color: #263445;
  }
</style>
""", unsafe_allow_html=True)



# ----------------- OAuth Callback -----------------
def ml_callback():
    code = st.query_params.get("code", [None])[0]
    if not code:
        st.error("⚠️ Código de autorização não encontrado.")
        return

    resp = requests.post(f"{BACKEND_URL}/auth/callback", json={"code": code})
    if not resp.ok:
        st.error(f"❌ Falha ao autenticar conta: {resp.status_code} — {resp.text}")
        return

    data = resp.json()
    salvar_tokens_no_banco(data)
    st.experimental_set_query_params(account=data["user_id"])
    st.session_state["conta"] = data["user_id"]
    st.success("✅ Conta ML autenticada com sucesso!")
    st.rerun()

# ----------------- Salvando Tokens -----------------
def salvar_tokens_no_banco(data: dict):
    try:
        with engine.connect() as conn:
            query = text("""
                INSERT INTO user_tokens (ml_user_id, access_token, refresh_token, expires_at)
                VALUES (:user_id, :access_token, :refresh_token, NOW() + interval '6 hours')
                ON CONFLICT (ml_user_id) DO UPDATE
                  SET access_token = EXCLUDED.access_token,
                      refresh_token = EXCLUDED.refresh_token,
                      expires_at   = NOW() + interval '6 hours';
            """)
            conn.execute(query, {
                "user_id":       data["user_id"],
                "access_token":  data["access_token"],
                "refresh_token": data["refresh_token"],
            })
    except Exception as e:
        st.error(f"❌ Erro ao salvar tokens no banco: {e}")

# ----------------- Carregamento de Vendas -----------------
@st.cache_data(ttl=300)
def carregar_vendas(conta_id: Optional[str] = None) -> pd.DataFrame:
    if conta_id:
        sql = text("""
            SELECT s.order_id,
                   s.date_adjusted,
                   s.item_id,
                   s.item_title,
                   s.status,
                   s.quantity,
                   s.unit_price,
                   s.total_amount,
                   s.ml_user_id,
                   s.buyer_nickname,
                   s.seller_sku,
                   s.custo_unitario,
                   s.quantity_sku,
                   s.ml_fee,
                   s.level1,
                   s.level2,
                   s.ads,
                   s.payment_id,
                   s.shipment_status,
                   s.shipment_substatus,
                   s.shipment_last_updated,
                   s.shipment_mode,
                   s.shipment_logistic_type,
                   s.shipment_list_cost,
                   s.shipment_delivery_type,
                   s.shipment_receiver_name,
                   s.shipment_delivery_sla,
                   s.order_cost,
                   s.base_cost,
                   s.shipment_cost,
                   s.frete_adjust,
                   u.nickname
              FROM sales s
              LEFT JOIN user_tokens u ON s.ml_user_id = u.ml_user_id
             WHERE s.ml_user_id = :uid
        """)
        df = pd.read_sql(sql, engine, params={"uid": conta_id})
    else:
        sql = text("""
            SELECT s.order_id,
                   s.date_adjusted,
                   s.item_id,
                   s.item_title,
                   s.status,
                   s.quantity,
                   s.unit_price,
                   s.total_amount,
                   s.ml_user_id,
                   s.buyer_nickname,
                   s.seller_sku,
                   s.custo_unitario,
                   s.quantity_sku,
                   s.ml_fee,
                   s.level1,
                   s.level2,
                   s.ads,
                   s.payment_id,
                   s.shipment_status,
                   s.shipment_substatus,
                   s.shipment_last_updated,
                   s.shipment_mode,
                   s.shipment_logistic_type,
                   s.shipment_list_cost,
                   s.shipment_delivery_type,
                   s.shipment_receiver_name,
                   s.shipment_delivery_sla,
                   s.order_cost,
                   s.base_cost,
                   s.shipment_cost,
                   s.frete_adjust,
                   u.nickname
              FROM sales s
              LEFT JOIN user_tokens u ON s.ml_user_id = u.ml_user_id
        """)
        df = pd.read_sql(sql, engine)

    return df

# ----------------- Componentes de Interface -----------------
from urllib.parse import urlencode

def render_add_account_button():
    login_url = f"{BACKEND_URL}/ml-login"
    st.markdown(f"""
    <button onclick="window.location.href='{login_url}';" style="
      background-color:#4CAF50;
      color:white;
      border:none;
      padding:10px;
      border-radius:5px;
      margin-bottom:10px;
    ">
      ➕ Adicionar Conta Mercado Livre
    </button>
    """, unsafe_allow_html=True)


from streamlit_option_menu import option_menu

def render_sidebar():
    # impede quebra de linha nos links
    st.sidebar.markdown("""
      <style>
        [data-testid="stSidebar"] .nav-link {
          white-space: nowrap !important;
          overflow: hidden !important;
          text-overflow: ellipsis !important;
        }
      </style>
    """, unsafe_allow_html=True)

    with st.sidebar:
        selected = option_menu(
            menu_title=None,
            options=["Dashboard", "Contas Cadastradas", "Relatórios", "Expedição"],
            icons=["house", "person-up", "file-earmark-text", "collection-fill"],
            menu_icon="list",
            default_index=[
                "Dashboard",
                "Contas Cadastradas",
                "Relatórios",
                "Expedição"
            ].index(st.session_state.get("page", "Dashboard")),
            orientation="vertical",
            styles={
                "container": {"padding": "0", "background-color": "#161b22"},
                "icon":      {"color": "#2ecc71", "font-size": "18px"},
                "nav-link":  {
                    "font-size": "16px",
                    "text-align": "left",
                    "margin": "4px 0",
                    "color": "#fff",
                    "background-color": "transparent",
                    "white-space": "nowrap"
                },
                "nav-link-selected": {
                    "background-color": "#2ecc71",
                    "color": "white"
                },
            },
        )
    st.session_state["page"] = selected
    return selected


# ----------------- Telas -----------------
import io  # no topo do seu script

def format_currency(value):
    """Formata valores para o padrão brasileiro."""
    return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def mostrar_dashboard():
    import time

    # --- sincroniza as vendas automaticamente apenas 1x ao carregar ---
    if "vendas_sincronizadas" not in st.session_state:
        with st.spinner("🔄 Sincronizando vendas..."):
            count = sync_all_accounts()
            st.cache_data.clear()
        placeholder = st.empty()
        with placeholder:
            st.success(f"{count} vendas novas sincronizadas com sucesso!")
            time.sleep(3)
        placeholder.empty()
        st.session_state["vendas_sincronizadas"] = True

    # --- carrega todos os dados ---
    df_full = carregar_vendas(None)
    if df_full.empty:
        st.warning("Nenhuma venda cadastrada.")
        return
        
    # ✅ TRADUZ STATUS AQUI
    from sales import traduzir_status
    df_full["status"] = df_full["status"].map(traduzir_status)

    # --- CSS para compactar inputs e remover espaços ---
    st.markdown(
        """
        <style>
        .stSelectbox > div, .stDateInput > div {
            padding-top: 0rem;
            padding-bottom: 0rem;
        }
        .stMultiSelect {
            max-height: 40px;
            overflow-y: auto;
        }
        .block-container {
            padding-top: 0rem;
        }
        .stMarkdown h1 { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # --- Filtro de contas fixo com checkboxes lado a lado + botão selecionar todos ---
    contas_df = pd.read_sql(text("SELECT nickname FROM user_tokens ORDER BY nickname"), engine)
    contas_lst = contas_df["nickname"].astype(str).tolist()
    
    st.markdown("**🧾 Contas Mercado Livre:**")


    # Estado para controlar se todas estão selecionadas
    if "todas_contas_marcadas" not in st.session_state:
        st.session_state["todas_contas_marcadas"] = True
    
    
    # Renderiza os checkboxes em colunas
    colunas_contas = st.columns(8)
    selecionadas = []
    
    for i, conta in enumerate(contas_lst):
        key = f"conta_{conta}"
        if key not in st.session_state:
            st.session_state[key] = st.session_state["todas_contas_marcadas"]
        if colunas_contas[i % 8].checkbox(conta, key=key):
            selecionadas.append(conta)
    
    # Aplica filtro
    if selecionadas:
        df_full = df_full[df_full["nickname"].isin(selecionadas)]


    # --- Linha única de filtros: Rápido | De | Até | Status ---
    col1, col2, col3, col4 = st.columns([1.5, 1.2, 1.2, 1.5])

    with col1:
        filtro_rapido = st.selectbox(
            "Filtrar Período",
            [
                "Período Personalizado",
                "Hoje",
                "Ontem",
                "Últimos 7 Dias",
                "Este Mês",
                "Últimos 30 Dias",
                "Este Ano"
            ],
            index=1,
            key="filtro_quick"
        )
    import pytz
    hoje = pd.Timestamp.now(tz="America/Sao_Paulo").date()
    data_min = df_full["date_adjusted"].dt.date.min()
    data_max = df_full["date_adjusted"].dt.date.max()
    
    if filtro_rapido == "Hoje":
        de = ate = min(hoje, data_max)
    elif filtro_rapido == "Ontem":
        de = ate = hoje - pd.Timedelta(days=1)
    elif filtro_rapido == "Últimos 7 Dias":
        de, ate = hoje - pd.Timedelta(days=7), hoje
    elif filtro_rapido == "Últimos 30 Dias":
        de, ate = hoje - pd.Timedelta(days=30), hoje
    elif filtro_rapido == "Este Mês":
        de, ate = hoje.replace(day=1), hoje
    elif filtro_rapido == "Este Ano":
        de, ate = hoje.replace(month=1, day=1), hoje
    else:
        de, ate = data_min, data_max
    
    custom = (filtro_rapido == "Período Personalizado")
    
    with col2:
        de = st.date_input("De", value=de, min_value=data_min, max_value=data_max, disabled=not custom, key="de_q")
    
    with col3:
        ate = st.date_input("Até", value=ate, min_value=data_min, max_value=data_max, disabled=not custom, key="ate_q")
    
    with col4:
        status_options = df_full["status"].dropna().unique().tolist()
        status_opcoes = ["Todos"] + status_options
        index_padrao = status_opcoes.index("Pago") if "Pago" in status_opcoes else 0
        status_selecionado = st.selectbox("Status", status_opcoes, index=index_padrao)
    
    # Aplica filtros finais
    df = df_full[
        (df_full["date_adjusted"].dt.date >= de) &
        (df_full["date_adjusted"].dt.date <= ate)
    ]
    if status_selecionado != "Todos":
        df = df[df["status"] == status_selecionado]

    
    # --- Filtros Avançados com checkbox dentro de Expander ---
    with st.expander("🔍 Filtros Avançados", expanded=False):
        # Atualiza as opções com base nos dados filtrados até aqui
        level1_opcoes = sorted(df["level1"].dropna().unique().tolist())
        st.markdown("**📂 Hierarquia 1**")
        col_l1 = st.columns(4)
        level1_selecionados = []
        for i, op in enumerate(level1_opcoes):
            if col_l1[i % 4].checkbox(op, key=f"level1_{op}"):
                level1_selecionados.append(op)
        if level1_selecionados:
            df = df[df["level1"].isin(level1_selecionados)]
    
        # Atualiza Level2 após Level1 aplicado
        level2_opcoes = sorted(df["level2"].dropna().unique().tolist())
        st.markdown("**📁 Hierarquia 2**")
        col_l2 = st.columns(4)
        level2_selecionados = []
        for i, op in enumerate(level2_opcoes):
            if col_l2[i % 4].checkbox(op, key=f"level2_{op}"):
                level2_selecionados.append(op)
        if level2_selecionados:
            df = df[df["level2"].isin(level2_selecionados)]
    
    # Verifica se há dados após os filtros
    if df.empty:
        st.warning("Nenhuma venda encontrada para os filtros selecionados.")
        st.stop()

    
    # Estilo customizado (CSS)
    st.markdown("""
        <style>
            .kpi-title {
                font-size: 15px;
                font-weight: 600;
                color: #000000;
                margin-bottom: 4px;
            }
            .kpi-value {
                font-size: 22px;
                font-weight: bold;
                color: #000000;
                line-height: 1.2;
                word-break: break-word;
            }
            .kpi-card {
                background-color: #ffffff;
                border-radius: 12px;
                padding: 16px 20px;
                margin: 5px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.06);
                border-left: 5px solid #27ae60;
            }
        </style>
    """, unsafe_allow_html=True)
    
    # Função para renderizar KPI card em coluna
    def kpi_card(col, title, value):
        col.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-title">{title}</div>
                <div class="kpi-value">{value}</div>
            </div>
        """, unsafe_allow_html=True)
    
    # Cálculos (ajustado)
    total_vendas        = len(df)
    total_valor         = df["total_amount"].sum()
    total_itens         = (df["quantity_sku"] * df["quantity"]).sum()
    ticket_venda        = total_valor / total_vendas if total_vendas else 0
    ticket_unidade      = total_valor / total_itens if total_itens else 0
    frete = -df["frete_adjust"].fillna(0).sum()
    taxa_mktplace       = df["ml_fee"].fillna(0).sum()
    cmv                 = ((df["quantity_sku"] * df["quantity"]) * df["custo_unitario"].fillna(0)).sum()
    margem_operacional  = total_valor - frete - taxa_mktplace - cmv
    # Colunas que não podem ficar vazias
    colunas_chk = ["seller_sku", "quantity_sku", "level1", "level2", "custo_unitario"]
    
    # Cria uma máscara booleana: True se **qualquer** uma das colunas estiver nula
    mask_faltantes = df[colunas_chk].isnull().any(axis=1)
    
    # Conta quantas linhas têm pelo menos um campo vazio
    sem_sku = mask_faltantes.sum()


    
    pct = lambda val: f"<span style='font-size: 70%; color: #666; display: inline-block; margin-left: 6px;'>({val / total_valor * 100:.1f}%)</span>" if total_valor else "<span style='font-size: 70%'>(0%)</span>"

    
    # Bloco 1: Indicadores Financeiros
    st.markdown("### 💼 Indicadores Financeiros")
    row1 = st.columns(5)
    kpi_card(row1[0], "💰 Faturamento", format_currency(total_valor))
    kpi_card(row1[1], "🚚 Frete Total", f"{format_currency(frete)} {pct(frete)}")
    kpi_card(row1[2], "📉 Taxa Marketplace", f"{format_currency(taxa_mktplace)} {pct(taxa_mktplace)}")
    kpi_card(row1[3], "📦 CMV", f"{format_currency(cmv)} {pct(cmv)}")
    kpi_card(row1[4], "💵 Margem Operacional", f"{format_currency(margem_operacional)} {pct(margem_operacional)}")
    
    # Bloco 2: Indicadores de Vendas
    st.markdown("### 📊 Indicadores de Vendas")
    row2 = st.columns(5)
    kpi_card(row2[0], "🧾 Vendas Realizadas", str(total_vendas))
    kpi_card(row2[1], "📦 Unidades Vendidas", str(int(total_itens)))
    kpi_card(row2[2], "🎯 Tkt Médio p/ Venda", format_currency(ticket_venda))
    kpi_card(row2[3], "🎯 Tkt Médio p/ Unid.", format_currency(ticket_unidade))
    kpi_card(row2[4], "❌ SKU Incompleto", str(sem_sku))
    
    import plotly.express as px

    # =================== Gráfico de Linha + Barra de Proporção ===================
    st.markdown("### 💵 Total Vendido por Período")
    
    # 🔘 Seletor de período + agrupamento + métrica lado a lado
    colsel1, colsel2, colsel3 = st.columns([1.2, 1.2, 1.6])

    
    with colsel1:
        st.markdown("**📆 Período**")
        tipo_visualizacao = st.radio(
            label="",
            options=["Diário", "Semanal", "Quinzenal", "Mensal"],
            horizontal=True,
            key="periodo"
        )
    
    with colsel2:
        st.markdown("**👥 Agrupamento**")
        modo_agregacao = st.radio(
            label="",
            options=["Por Conta", "Total Geral"],
            horizontal=True,
            key="modo_agregacao"
        )

    with colsel3:
        st.markdown("**📏 Métrica da Barra**")
        metrica_barra = st.radio(
            "Métrica",
            ["Faturamento", "Qtd. Vendas", "Qtd. Unidades"],
            horizontal=True,
            key="metrica_barra"
        )


    
    df_plot = df.copy()
    
    # Define bucket de datas
    if de == ate:
        df_plot["date_bucket"] = df_plot["date_adjusted"].dt.floor("h")
        periodo_label = "Hora"
    else:
        if tipo_visualizacao == "Diário":
            df_plot["date_bucket"] = df_plot["date_adjusted"].dt.date
            periodo_label = "Dia"
        elif tipo_visualizacao == "Semanal":
            df_plot["date_bucket"] = df_plot["date_adjusted"].dt.to_period("W").apply(lambda p: p.start_time.date())
            periodo_label = "Semana"
        elif tipo_visualizacao == "Quinzenal":
            df_plot["quinzena"] = df_plot["date_adjusted"].apply(
                lambda d: f"{d.year}-Q{(d.month-1)*2//30 + 1}-{1 if d.day <= 15 else 2}"
            )
            df_plot["date_bucket"] = df_plot["quinzena"]
            periodo_label = "Quinzena"
        else:
            df_plot["date_bucket"] = df_plot["date_adjusted"].dt.to_period("M").astype(str)
            periodo_label = "Mês"
    
    # Agrupamento e definição de cores
    if modo_agregacao == "Por Conta":
        vendas_por_data = (
            df_plot.groupby(["date_bucket", "nickname"])["total_amount"]
            .sum()
            .reset_index(name="Valor Total")
        )
        color_dim = "nickname"
    
        total_por_conta = (
            df_plot.groupby("nickname")["total_amount"]
            .sum()
            .reset_index(name="total")
            .sort_values("total", ascending=False)
        )
    
        color_palette = px.colors.sequential.Agsunset
        nicknames = total_por_conta["nickname"].tolist()
        color_map = {nick: color_palette[i % len(color_palette)] for i, nick in enumerate(nicknames)}
    
    else:
        vendas_por_data = (
            df_plot.groupby("date_bucket")["total_amount"]
            .sum()
            .reset_index(name="Valor Total")
        )
        color_dim = None
        color_map = None  # Não será usado
        total_por_conta = None
    
    # 🔢 Gráfico(s)
    if modo_agregacao == "Por Conta":
        col1, col2 = st.columns([4, 1])
    else:
        col1 = st.container()
        col2 = None
    
    # 📈 Gráfico de Linha
    with col1:
        fig = px.line(
            vendas_por_data,
            x="date_bucket",
            y="Valor Total",
            color=color_dim,
            labels={"date_bucket": periodo_label, "Valor Total": "Valor Total", "nickname": "Conta"},
            color_discrete_map=color_map,
        )
        fig.update_traces(mode="lines+markers", marker=dict(size=5))
        fig.update_layout(
            margin=dict(t=20, b=20, l=40, r=10),
            showlegend=True
        )
        st.plotly_chart(fig, use_container_width=True)
    
    # 📊 Gráfico de barra proporcional (somente se Por Conta)
    if modo_agregacao == "Por Conta" and not total_por_conta.empty:

    
        if metrica_barra == "Faturamento":
            base = (
                df_plot.groupby("nickname")["total_amount"]
                .sum()
                .reset_index(name="valor")
            )
        elif metrica_barra == "Qtd. Vendas":
            base = (
                df_plot.groupby("nickname")
                .size()
                .reset_index(name="valor")
            )
        else:  # Qtd. Unidades
            base = (
                df_plot.groupby("nickname")
                .apply(lambda x: (x["quantity_sku"] * x["quantity"]).sum())
                .reset_index(name="valor")
            )
    
        base = base.sort_values("valor", ascending=False)
        base["percentual"] = base["valor"] / base["valor"].sum()
    
        # 🏷️ Texto das barras
        def formatar_valor(v):
            if metrica_barra == "Faturamento":
                return f"R$ {v:,.0f}".replace(",", "v").replace(".", ",").replace("v", ".")
            elif metrica_barra == "Qtd. Vendas":
                return f"{int(v)} vendas"
            else:
                return f"{int(v)} unid."
    
        base["texto"] = base.apply(
            lambda row: f"{row['percentual']:.0%} ({formatar_valor(row['valor'])})", axis=1
        )
        base["grupo"] = "Contas"
    
        fig_bar = px.bar(
            base,
            x="grupo",
            y="percentual",
            color="nickname",
            text="texto",
            color_discrete_map=color_map,
        )
    
        fig_bar.update_layout(
            yaxis=dict(title=None, tickformat=".0%", range=[0, 1]),
            xaxis=dict(title=None),
            showlegend=False,
            margin=dict(t=20, b=20, l=10, r=10),
            height=400
        )
    
        fig_bar.update_traces(
            textposition="inside",
            insidetextanchor="middle",
            textfont=dict(color="white", size=12)
        )
    
        with col2:
            st.plotly_chart(fig_bar, use_container_width=True)




    # === Gráfico de barras: Média por dia da semana ===
    st.markdown('<div class="section-title">📅 Vendas por Dia da Semana</div>', unsafe_allow_html=True)
    
    # Nome dos dias na ordem certa
    dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
    
    # Extrai dia da semana em português
    df["dia_semana"] = df["date_adjusted"].dt.day_name().map({
        "Monday": "Segunda", "Tuesday": "Terça", "Wednesday": "Quarta",
        "Thursday": "Quinta", "Friday": "Sexta", "Saturday": "Sábado", "Sunday": "Domingo"
    })
    
    # Extrai a data (sem hora)
    df["data"] = df["date_adjusted"].dt.date
    
    # Soma o total vendido por dia (independente da hora)
    total_por_data = df.groupby(["dia_semana", "data"])["total_amount"].sum().reset_index()
    
    # Agora calcula a média por dia da semana
    media_por_dia = total_por_data.groupby("dia_semana")["total_amount"].mean().reindex(dias).reset_index()
    
    # Plota o gráfico de barras
    fig_bar = px.bar(
        media_por_dia,
        x="dia_semana",
        y="total_amount",
        text_auto=".2s",
        labels={"dia_semana": "Dia da Semana", "total_amount": "Média Vendida (R$)"},
        color_discrete_sequence=["#27ae60"]
    )
    
    st.plotly_chart(fig_bar, use_container_width=True, theme="streamlit")




    # =================== Gráfico de Linha - Faturamento Acumulado por Hora ===================
    st.markdown("### ⏰ Faturamento Acumulado por Hora do Dia (Média)")
    
    # Extrai hora e data
    df["hora"] = df["date_adjusted"].dt.hour
    df["data"] = df["date_adjusted"].dt.date
    
    # Soma o total vendido por hora e por dia
    vendas_por_dia_e_hora = df.groupby(["data", "hora"])["total_amount"].sum().reset_index()
    
    # Garante que todas as horas estejam presentes para todos os dias
    todos_dias = vendas_por_dia_e_hora["data"].unique()
    todas_horas = list(range(0, 24))
    malha_completa = pd.MultiIndex.from_product([todos_dias, todas_horas], names=["data", "hora"])
    vendas_completa = vendas_por_dia_e_hora.set_index(["data", "hora"]).reindex(malha_completa, fill_value=0).reset_index()
    
    # Acumula por hora dentro de cada dia
    vendas_completa["acumulado_dia"] = vendas_completa.groupby("data")["total_amount"].cumsum()
    
    # Agora calcula a média acumulada por hora (entre os dias)
    media_acumulada_por_hora = (
        vendas_completa
        .groupby("hora")["acumulado_dia"]
        .mean()
        .reset_index(name="Valor Médio Acumulado")
    )
    
    # Verifica se é filtro de hoje
    hoje = pd.Timestamp.now(tz="America/Sao_Paulo").date()
    filtro_hoje = (de == ate) and (de == hoje)
    
    if filtro_hoje:
        hora_atual = pd.Timestamp.now(tz="America/Sao_Paulo").hour
        df_hoje = df[df["data"] == hoje]
        vendas_hoje_por_hora = (
            df_hoje.groupby("hora")["total_amount"].sum().reindex(range(24), fill_value=0)
            .cumsum()
            .reset_index(name="Valor Médio Acumulado")
            .rename(columns={"index": "hora"})
        )
        # Traz o ponto até hora atual
        ponto_extra = pd.DataFrame([{
            "hora": hora_atual,
            "Valor Médio Acumulado": vendas_hoje_por_hora.loc[hora_atual, "Valor Médio Acumulado"]
        }])
        media_acumulada_por_hora = pd.concat([media_acumulada_por_hora, ponto_extra]).groupby("hora").last().reset_index()
    
    else:
        # Para histórico, adiciona o ponto final às 23h com média total diária
        media_final = df.groupby("data")["total_amount"].sum().mean()
        ponto_final = pd.DataFrame([{
            "hora": 23,
            "Valor Médio Acumulado": media_final
        }])
        media_acumulada_por_hora = pd.concat([media_acumulada_por_hora, ponto_final]).groupby("hora").last().reset_index()
    
    # Plota o gráfico
    fig_hora = px.line(
        media_acumulada_por_hora,
        x="hora",
        y="Valor Médio Acumulado",
        title="⏰ Faturamento Acumulado por Hora (Média por Dia)",
        labels={
            "hora": "Hora do Dia",
            "Valor Médio Acumulado": "Valor Acumulado (R$)"
        },
        color_discrete_sequence=["#27ae60"],
        markers=True
    )
    fig_hora.update_layout(xaxis=dict(dtick=1))
    
    st.plotly_chart(fig_hora, use_container_width=True)

import time
from datetime import datetime, timedelta
import pandas as pd
import streamlit as st
from sqlalchemy import text

from db import engine
from reconcile import reconciliar_vendas

def mostrar_contas_cadastradas():
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 0rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.header("🏷️ Contas Cadastradas")
    render_add_account_button()

    df = pd.read_sql(text("SELECT ml_user_id, nickname, access_token, refresh_token FROM user_tokens ORDER BY nickname"), engine)

    if df.empty:
        st.warning("Nenhuma conta cadastrada.")
        return

    st.markdown("### 🔧 Reconciliação de Vendas")

    # — 1) Modo de reconciliação —
    modo = st.radio(
        "🔄 Modo de reconciliação",
        ("Período", "Dia único"),
        index=0,
        key="modo_reconciliacao"
    )

    # — 2) Inputs de data conforme o modo —
    if modo == "Período":
        col1, col2 = st.columns(2)
        with col1:
            data_inicio = st.date_input(
                "📅 Data inicial",
                value=datetime.today() - timedelta(days=180),
                key="dt_inicio"
            )
        with col2:
            data_fim = st.date_input(
                "📅 Data final",
                value=datetime.today(),
                key="dt_fim"
            )
    else:
        data_unica = st.date_input(
            "📅 Escolha o dia",
            value=datetime.today(),
            key="dt_unico"
        )

    # — 3) Multiselect único de contas —
    contas_dict = (
        df[["nickname", "ml_user_id"]]
        .drop_duplicates()
        .set_index("nickname")["ml_user_id"]
        .astype(str)
        .to_dict()
    )
    contas_selecionadas = st.multiselect(
        "🏢 Escolha as contas para reconciliar",
        options=list(contas_dict.keys()),
        default=list(contas_dict.keys()),
        key="contas"
    )

    # — 4) Botão único para executar —
    if st.button("🧹 Reconciliar", use_container_width=True):
        if not contas_selecionadas:
            st.warning("⚠️ Nenhuma conta selecionada.")
            return

        # Define intervalo de datas
        if modo == "Período":
            desde = datetime.combine(data_inicio, datetime.min.time())
            ate   = datetime.combine(data_fim,   datetime.max.time())
        else:
            desde = datetime.combine(data_unica, datetime.min.time())
            ate   = datetime.combine(data_unica, datetime.max.time())

        # Loop de reconciliação
        contas_df = df[df["nickname"].isin(contas_selecionadas)]
        total = len(contas_df)
        progresso = st.progress(0, text="🔁 Iniciando...")
        atualizadas = erros = 0

        for i, row in enumerate(contas_df.itertuples(index=False), start=1):
            st.write(f"🔍 Conta **{row.nickname}**")
            res = reconciliar_vendas(
                ml_user_id=str(row.ml_user_id),
                desde=desde,
                ate=ate
            )
            atualizadas += res["atualizadas"]
            erros       += res["erros"]
            progresso.progress(i/total, text=f"⏳ {i}/{total}")
            time.sleep(0.05)

        progresso.empty()
        st.success(f"✅ Concluído: {atualizadas} atualizações, {erros} erros.")

    # --- Seção por conta individual ---
    for row in df.itertuples(index=False):
        with st.expander(f"🔗 Conta ML: {row.nickname}"):
            ml_user_id = str(row.ml_user_id)
            access_token = row.access_token
            refresh_token = row.refresh_token
    
            st.write(f"**User ID:** `{ml_user_id}`")
            st.write(f"**Access Token:** `{access_token}`")
            st.write(f"**Refresh Token:** `{refresh_token}`")


def mostrar_relatorios():
    import time
    import pytz
    from sales import traduzir_status

    # --- CSS de espaçamento ---
    st.markdown("""
        <style>
        .block-container { padding-top: 0rem; }
        .stSelectbox > div, .stDateInput > div { padding-top: 0; padding-bottom: 0; }
        .stMultiSelect { max-height: 40px; overflow-y: auto; }
        </style>
    """, unsafe_allow_html=True)

    st.header("📋 Relatórios de Vendas")

    # --- carga e tradução de status ---
    df_full = carregar_vendas(None)
    if df_full.empty:
        st.warning("Nenhum dado encontrado.")
        return
    df_full["status"] = df_full["status"].map(traduzir_status)
    df_full["date_adjusted"] = pd.to_datetime(df_full["date_adjusted"])

    # --- Filtro de Contas Lado a Lado ---
    contas_df   = pd.read_sql(text("SELECT nickname FROM user_tokens ORDER BY nickname"), engine)
    contas_lst  = contas_df["nickname"].tolist()
    st.markdown("**🧾 Contas Mercado Livre:**")
    if "todas_contas_marcadas" not in st.session_state:
        st.session_state["todas_contas_marcadas"] = True
    cols = st.columns(8)
    selecionadas = []
    for i, conta in enumerate(contas_lst):
        key = f"rel_conta_{conta}"
        if key not in st.session_state:
            st.session_state[key] = st.session_state["todas_contas_marcadas"]
        if cols[i % 8].checkbox(conta, key=key):
            selecionadas.append(conta)
    if selecionadas:
        df_full = df_full[df_full["nickname"].isin(selecionadas)]

    # --- Filtro Rápido | De | Até | Status ---
    col1, col2, col3, col4 = st.columns([1.5,1.2,1.2,1.5])
    hoje      = pd.Timestamp.now(tz="America/Sao_Paulo").date()
    data_min  = df_full["date_adjusted"].dt.date.min()
    data_max  = df_full["date_adjusted"].dt.date.max()

    with col1:
        filtro = st.selectbox(
            "📅 Período",
            ["Personalizado","Hoje","Ontem","Últimos 7 Dias","Este Mês","Últimos 30 Dias","Este Ano"],
            index=1, key="rel_filtro_quick"
        )
    if filtro == "Hoje":
        de = ate = min(hoje, data_max)
    elif filtro == "Ontem":
        de = ate = hoje - pd.Timedelta(days=1)
    elif filtro == "Últimos 7 Dias":
        de, ate = hoje - pd.Timedelta(days=7), hoje
    elif filtro == "Últimos 30 Dias":
        de, ate = hoje - pd.Timedelta(days=30), hoje
    elif filtro == "Este Mês":
        de, ate = hoje.replace(day=1), hoje
    elif filtro == "Este Ano":
        de, ate = hoje.replace(month=1, day=1), hoje
    else:
        de, ate = data_min, data_max

    custom = (filtro == "Personalizado")
    with col2:
        de = st.date_input("De",  value=de,  min_value=data_min, max_value=data_max, disabled=not custom, key="rel_de")
    with col3:
        ate= st.date_input("Até", value=ate, min_value=data_min, max_value=data_max, disabled=not custom, key="rel_ate")
    with col4:
        opts = ["Todos"] + df_full["status"].dropna().unique().tolist()
        idx  = opts.index("Pago") if "Pago" in opts else 0
        status_sel = st.selectbox("Status", opts, index=idx, key="rel_status")

    df = df_full[
        (df_full["date_adjusted"].dt.date >= de) &
        (df_full["date_adjusted"].dt.date <= ate)
    ]
    if status_sel != "Todos":
        df = df[df["status"] == status_sel]

    # --- Filtros Avançados: Hierarquia 1 e 2 ---
    with st.expander("🔍 Filtros Avançados", expanded=False):
        # Hierarquia 1
        l1_opts = sorted(df["level1"].dropna().unique())
        st.markdown("**📂 Hierarquia 1**")
        cols1 = st.columns(4)
        sel1 = [op for i,op in enumerate(l1_opts) if cols1[i%4].checkbox(op, key=f"rel_l1_{op}")]
        if sel1:
            df = df[df["level1"].isin(sel1)]
        # Hierarquia 2
        l2_opts = sorted(df["level2"].dropna().unique())
        st.markdown("**📁 Hierarquia 2**")
        cols2 = st.columns(4)
        sel2 = [op for i,op in enumerate(l2_opts) if cols2[i%4].checkbox(op, key=f"rel_l2_{op}")]
        if sel2:
            df = df[df["level2"].isin(sel2)]

    if df.empty:
        st.warning("Nenhuma venda após filtros.")
        return

    # --- Ordena por timestamp completo ---
    df = df.sort_values("date_adjusted", ascending=False).copy()

    # --- Monta colunas finais ---
    df["Data"]                   = df["date_adjusted"].dt.strftime("%d/%m/%Y %H:%M:%S")
    df["ID DA VENDA"]            = df["order_id"]
    df["CONTA"]                  = df["nickname"]
    df["TÍTULO DO ANÚNCIO"]      = df["item_title"]
    df["SKU DO PRODUTO"]         = df["seller_sku"]
    df["HIERARQUIA 1"]           = df["level1"]
    df["HIERARQUIA 2"]           = df["level2"]
    df["QUANTIDADE DO SKU"]      = df["quantity_sku"].fillna(0).astype(int)
    df["VALOR DA VENDA"]         = df["total_amount"]
    df["TAXA DA PLATAFORMA"]     = df["ml_fee"].fillna(0)
    df["CUSTO DE FRETE"]         = df["frete_adjust"].fillna(0)
    df["CMV"]                    = (
        df["quantity_sku"].fillna(0)
        * df["quantity"].fillna(0)
        * df["custo_unitario"].fillna(0)
    )
    df["MARGEM DE CONTRIBUIÇÃO"] = (
        df["VALOR DA VENDA"]
        - df["TAXA DA PLATAFORMA"]
        - df["CUSTO DE FRETE"]
        - df["CMV"]
    )

    cols_final = [
        "ID DA VENDA","CONTA","Data","TÍTULO DO ANÚNCIO","SKU DO PRODUTO",
        "HIERARQUIA 1","HIERARQUIA 2","QUANTIDADE DO SKU","VALOR DA VENDA",
        "TAXA DA PLATAFORMA","CUSTO DE FRETE","CMV","MARGEM DE CONTRIBUIÇÃO"
    ]
    st.dataframe(df[cols_final], use_container_width=True)

def mostrar_expedicao_logistica(df: pd.DataFrame):
    import streamlit as st
    import plotly.express as px
    import pandas as pd
    from io import BytesIO
    import base64
    from datetime import datetime
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle, PageBreak
    )
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib import colors
    import pytz
    from sales import traduzir_status

    # Estilo
    st.markdown(  
        """
        <style>
        .block-container { padding-top: 0rem; }
        </style>
        """, unsafe_allow_html=True
    )
    st.header("🚚 Expedição e Logística")

    if df.empty:
        st.warning("Nenhum dado encontrado.")
        return

    # === Mapeamentos e cálculos iniciais ===
    def mapear_tipo(valor):
        match valor:
            case 'fulfillment': return 'FULL'
            case 'self_service': return 'FLEX'
            case 'drop_off': return 'Correios'
            case 'xd_drop_off': return 'Agência'
            case 'cross_docking': return 'Coleta'
            case 'me2': return 'Envio Padrão'
            case _: return 'outros'

    df["Tipo de Envio"] = df["shipment_logistic_type"].apply(mapear_tipo)

    # Garantir que 'shipment_delivery_sla' esteja em datetime
    if "shipment_delivery_sla" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["shipment_delivery_sla"]):
        df["shipment_delivery_sla"] = pd.to_datetime(df["shipment_delivery_sla"], errors="coerce")

    # Cálculo de quantidade
    if "quantity" in df.columns and "quantity_sku" in df.columns:
        df["quantidade"] = df["quantity"] * df["quantity_sku"]
    else:
        st.error("Colunas 'quantity' e/ou 'quantity_sku' não encontradas.")
        st.stop()

    # Data da venda
    if "date_adjusted" not in df.columns:
        st.error("Coluna 'date_adjusted' não encontrada.")
        st.stop()
    df["data_venda"] = pd.to_datetime(df["date_adjusted"]).dt.date

    # Conversão para fuso de SP
    def _to_sp_date(x):
        if pd.isna(x):
            return pd.NaT
        ts = pd.to_datetime(x, utc=True)
        return ts.tz_convert("America/Sao_Paulo").date()

    if "shipment_delivery_sla" in df.columns:
        df["shipment_delivery_sla"] = pd.to_datetime(df["shipment_delivery_sla"], utc=True, errors="coerce")
        df["data_limite"] = df["shipment_delivery_sla"].apply(
            lambda x: x.tz_convert("America/Sao_Paulo").date() if pd.notnull(x) else pd.NaT
        )
    else:
        df["data_limite"] = pd.NaT
    
    import pytz
    hoje = pd.Timestamp.now(tz="America/Sao_Paulo").date()

    data_min_venda = df["data_venda"].dropna().min()
    data_max_venda = df["data_venda"].dropna().max()

    data_min_limite = df["data_limite"].dropna().min()
    data_max_limite = df["data_limite"].dropna().max()
    if pd.isna(data_min_limite):
        data_min_limite = hoje
    if pd.isna(data_max_limite) or data_max_limite < data_min_limite:
        data_max_limite = data_min_limite + pd.Timedelta(days=7)

    # === UNIFICADO 1: Datas (Venda + Expedição) + Filtro de Período (só Expedição) ===
    
    # --- Linha 1: Período + Despacho Limite ---
    col1, col2, col3 = st.columns([1.5, 1.2, 1.2])
    
    with col1:
        periodo = st.selectbox(
            "Filtrar Período de Expedição",
            [
                "Período Personalizado",
                "Hoje",
                "Amanhã",
                "Ontem",
                "Próximos 7 Dias",
                "Este Mês",
                "Próximos 30 Dias",
                "Este Ano"
            ],
            index=1,
            key="filtro_expedicao_periodo"
        )
    
        # Define intervalo padrão com base no filtro
    import pytz
    hoje = pd.Timestamp.now(tz="America/Sao_Paulo").date()
    if periodo == "Hoje":
        de_limite_default = ate_limite_default = min(hoje, data_max_limite)
    elif periodo == "Amanhã":
        de_limite_default = ate_limite_default = hoje + pd.Timedelta(days=1)
    elif periodo == "Ontem":
        de_limite_default = ate_limite_default = hoje - pd.Timedelta(days=1)
    elif periodo == "Próximos 7 Dias":
        de_limite_default, ate_limite_default = hoje, hoje + pd.Timedelta(days=6)
    elif periodo == "Próximos 30 Dias":
        de_limite_default, ate_limite_default = hoje, hoje + pd.Timedelta(days=29)
    elif periodo == "Este Mês":
        de_limite_default, ate_limite_default = hoje.replace(day=1), hoje
    elif periodo == "Este Ano":
        de_limite_default, ate_limite_default = hoje.replace(month=1, day=1), hoje
    else:
        de_limite_default, ate_limite_default = data_min_limite, data_max_limite


    # Ajuste para não extrapolar as datas mínimas/máximas disponíveis
    de_limite_default = max(de_limite_default, data_min_limite)
    de_limite_default = min(de_limite_default, data_max_limite)
    ate_limite_default = max(ate_limite_default, data_min_limite)
    ate_limite_default = min(ate_limite_default, data_max_limite)
    
    modo_personalizado = (periodo == "Período Personalizado")
    
    with col2:
        de_limite = st.date_input(
            "Despacho Limite de:",
            value=de_limite_default,
            min_value=data_min_limite,
            max_value=data_max_limite,
            disabled=not modo_personalizado
        )
    with col3:
        ate_limite = st.date_input(
            "Despacho Limite até:",
            value=ate_limite_default,
            min_value=data_min_limite,
            max_value=data_max_limite,
            disabled=not modo_personalizado
        )
    
    if not modo_personalizado:
        de_limite = de_limite_default
        ate_limite = ate_limite_default
    
    # --- Linha 2: Venda de / até ---
    col_v1, col_v2 = st.columns(2)
    
    with col_v1:
        de_venda = st.date_input(
            "Venda de:",
            value=data_min_venda,
            min_value=data_min_venda,
            max_value=data_max_venda,
            key="data_venda_de"
        )
    with col_v2:
        ate_venda = st.date_input(
            "Venda até:",
            value=data_max_venda,
            min_value=data_min_venda,
            max_value=data_max_venda,
            key="data_venda_ate"
        )

    # --- Aplicar filtro por data de venda e expedição no DataFrame base ---
    df = df[
        (df["data_venda"] >= de_venda) & (df["data_venda"] <= ate_venda) &
        (df["data_limite"].isna() |
         ((df["data_limite"] >= de_limite) & (df["data_limite"] <= ate_limite)))
    ]

    df_filtrado = df.copy()
    
    # --- Linha 3: Conta, Status, Status Envio, Tipo de Envio ---
    col6, col7, col8 = st.columns(3)
    
    with col6:
        contas = df["nickname"].dropna().unique().tolist()
        conta = st.selectbox("Conta", ["Todos"] + sorted(contas))
    
    with col7:
        status_traduzido = sorted(df["status"].dropna().unique().tolist())
        status_ops = ["Todos"] + status_traduzido
        index_padrao = status_ops.index("Pago") if "Pago" in status_ops else 0
        status = st.selectbox("Status", status_ops, index=index_padrao)
    
    with col8:
        status_data_envio = st.selectbox(
            "Status Envio",
            ["Todos", "Com Data de Envio", "Sem Data de Envio"],
            index=1
        )
    

    # --- Aplicar filtros restantes ---

    if conta != "Todos":
        df_filtrado = df_filtrado[df_filtrado["nickname"] == conta]
    if status != "Todos":
        df_filtrado = df_filtrado[df_filtrado["status"] == status]
    if status_data_envio == "Com Data de Envio":
        df_filtrado = df_filtrado[df_filtrado["data_limite"].notna()]
    elif status_data_envio == "Sem Data de Envio":
        df_filtrado = df_filtrado[df_filtrado["data_limite"].isna()]
    
    
    # Aqui entra o bloco com os filtros de hierarquia
    with st.expander("🔍 Filtros Avançados", expanded=False):

        # Tipo de Envio (Checkboxes)
        tipo_envio_opcoes = sorted(df_filtrado["Tipo de Envio"].dropna().unique().tolist())
        st.markdown("**🚚 Tipo de Envio**")
        col_envio = st.columns(4)
        tipo_envio_selecionados = []
        for i, op in enumerate(tipo_envio_opcoes):
            if col_envio[i % 4].checkbox(op, key=f"tipo_envio_{op}"):
                tipo_envio_selecionados.append(op)
        if tipo_envio_selecionados:
            df_filtrado = df_filtrado[df_filtrado["Tipo de Envio"].isin(tipo_envio_selecionados)]
    
        # Hierarquia 1
        level1_opcoes = sorted(df_filtrado["level1"].dropna().unique().tolist())
        st.markdown("**📂 Hierarquia 1**")
        col_l1 = st.columns(4)
        level1_selecionados = []
        for i, op in enumerate(level1_opcoes):
            if col_l1[i % 4].checkbox(op, key=f"filtros_level1_{op}"):
                level1_selecionados.append(op)
        if level1_selecionados:
            df_filtrado = df_filtrado[df_filtrado["level1"].isin(level1_selecionados)]
    
        # Hierarquia 2
        level2_opcoes = sorted(df_filtrado["level2"].dropna().unique().tolist())
        st.markdown("**📁 Hierarquia 2**")
        col_l2 = st.columns(4)
        level2_selecionados = []
        for i, op in enumerate(level2_opcoes):
            if col_l2[i % 4].checkbox(op, key=f"filtros_level2_{op}"):
                level2_selecionados.append(op)
        if level2_selecionados:
            df_filtrado = df_filtrado[df_filtrado["level2"].isin(level2_selecionados)]


    # Verificação final
    if df_filtrado.empty:
        st.warning("Nenhum dado encontrado com os filtros aplicados.")
        return


    df_filtrado = df_filtrado.copy()
    df_filtrado["Canal de Venda"] = "MERCADO LIVRE"
    
    df_filtrado["Data Limite do Envio"] = df_filtrado["data_limite"].apply(
        lambda d: d.strftime("%d/%m/%Y") if pd.notna(d) else "—"
    )


    tabela = df_filtrado[[
        "order_id",                  
        "shipment_receiver_name",    
        "nickname",                  
        "Tipo de Envio",            
        "quantidade",              
        "level1",                    
        "Data Limite do Envio"     
    ]].rename(columns={
        "order_id": "ID VENDA",
        "shipment_receiver_name": "NOME CLIENTE",
        "nickname": "CONTA",
        "Tipo de Envio": "TIPO DE ENVIO",
        "quantidade": "QUANTIDADE",
        "level1": "PRODUTO [HIERARQUIA 1]",
        "Data Limite do Envio": "DATA DE ENVIO"
    })

    
    # Ordenar pela quantidade em ordem decrescente
    tabela = tabela.sort_values(by="QUANTIDADE", ascending=False)
    
    # === KPIs ===
    total_vendas = len(df_filtrado)
    total_quantidade = int(df_filtrado["quantidade"].sum())
    
    k1, k2 = st.columns(2)
    with k1:
        st.metric(label="Total de Vendas Filtradas", value=f"{total_vendas:,}")
    with k2:
        st.metric(label="Quantidade Total", value=f"{total_quantidade:,}")
    
    # em seguida exibe a tabela
    st.markdown("### 📋 Tabela de Expedição por Venda")
    st.dataframe(tabela, use_container_width=True, height=500)

    df_grouped = df_filtrado.groupby("level1", as_index=False).agg({"quantidade": "sum"})
    df_grouped = df_grouped.rename(columns={"level1": "Hierarquia 1", "quantidade": "Quantidade"})
    
    # Ordenar do maior para o menor
    df_grouped = df_grouped.sort_values(by="Quantidade", ascending=False)
    
    fig_bar = px.bar(
        df_grouped,
        x="Hierarquia 1",
        y="Quantidade",
        text="Quantidade",  # Adiciona o rótulo
        barmode="group",
        height=400,
        color_discrete_sequence=["#2ECC71"]
    )
    
    # Ajustar posição dos rótulos (em cima)
    fig_bar.update_traces(textposition="outside")
    
    # Ajustar layout para não cortar os rótulos
    fig_bar.update_layout(uniformtext_minsize=8, uniformtext_mode='hide', margin=dict(t=40, b=40))
    
    st.plotly_chart(fig_bar, use_container_width=True)


    # === TABELAS LADO A LADO COM UNIDADES E VENDAS ===
    st.markdown("### 📊 Resumo por Agrupamento")
    col_r1, col_r2, col_r3 = st.columns(3)
    
    # ===== Tabela 1: Hierarquia 1 =====
    df_h1 = (
        df_filtrado
        .groupby("level1", as_index=False)
        .agg(
            Quantidade_Unidades=("quantidade", "sum"),
            Quantidade_de_Vendas=("order_id", "nunique")
        )
        .rename(columns={"level1": "Hierarquia 1"})
    )
    # totais
    tot_q1 = df_h1["Quantidade_Unidades"].sum()
    tot_v1 = df_h1["Quantidade_de_Vendas"].sum()
    df_h1 = pd.concat([
        df_h1,
        pd.DataFrame({
            "Hierarquia 1": ["Total"],
            "Quantidade_Unidades": [tot_q1],
            "Quantidade_de_Vendas": [tot_v1]
        })
    ], ignore_index=True)
    with col_r1:
        st.dataframe(df_h1, use_container_width=True, hide_index=True)
    
    # ===== Tabela 2: Hierarquia 2 =====
    df_h2 = (
        df_filtrado
        .groupby("level2", as_index=False)
        .agg(
            Quantidade_Unidades=("quantidade", "sum"),
            Quantidade_de_Vendas=("order_id", "nunique")
        )
        .rename(columns={"level2": "Hierarquia 2"})
    )
    tot_q2 = df_h2["Quantidade_Unidades"].sum()
    tot_v2 = df_h2["Quantidade_de_Vendas"].sum()
    df_h2 = pd.concat([
        df_h2,
        pd.DataFrame({
            "Hierarquia 2": ["Total"],
            "Quantidade_Unidades": [tot_q2],
            "Quantidade_de_Vendas": [tot_v2]
        })
    ], ignore_index=True)
    with col_r2:
        st.dataframe(df_h2, use_container_width=True, hide_index=True)
    
    # ===== Tabela 3: Tipo de Envio =====
    df_tipo = (
        df_filtrado
        .groupby("Tipo de Envio", as_index=False)
        .agg(
            Quantidade_Unidades=("quantidade", "sum"),
            Quantidade_de_Vendas=("order_id", "nunique")
        )
    )
    tot_qt = df_tipo["Quantidade_Unidades"].sum()
    tot_vt = df_tipo["Quantidade_de_Vendas"].sum()
    df_tipo = pd.concat([
        df_tipo,
        pd.DataFrame({
            "Tipo de Envio": ["Total"],
            "Quantidade_Unidades": [tot_qt],
            "Quantidade_de_Vendas": [tot_vt]
        })
    ], ignore_index=True)
    with col_r3:
        st.dataframe(df_tipo, use_container_width=True, hide_index=True)


    def gerar_relatorio_pdf(
        tabela_df, df_h1, df_h2, df_tipo,
        periodo_venda, periodo_expedicao
    ):
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4,
                                leftMargin=20, rightMargin=20,
                                topMargin=20, bottomMargin=20)
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            name="CenteredTitle",
            parent=styles["Title"],
            alignment=TA_CENTER,
            fontSize=16
        )
        normal = styles["Normal"]
        elems = []
    
        # --- Cabeçalho ---
        try:
            logo = Image("favicon.png", width=50, height=50)
        except:
            logo = Paragraph("", normal)
        titulo = Paragraph("Relatório de Expedição e Logística", title_style)
        header = Table([[logo, titulo]], colWidths=[60, 460])
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN",  (1, 0), (1, 0), "CENTER"),
            ("LEFTPADDING",  (0,0), (-1,-1), 0),
            ("RIGHTPADDING", (0,0), (-1,-1), 0),
        ]))
        elems.append(header)
        elems.append(Spacer(1, 8))
    
        # --- Períodos ---
        txt = (
            f"<b>Venda:</b> {periodo_venda[0].strftime('%d/%m/%Y')} ↔ {periodo_venda[1].strftime('%d/%m/%Y')}<br/>"
            f"<b>Expedição:</b> {periodo_expedicao[0].strftime('%d/%m/%Y')} ↔ {periodo_expedicao[1].strftime('%d/%m/%Y')}"
        )
        elems.append(Paragraph(txt, normal))
        elems.append(Spacer(1, 12))
    
        # --- KPIs em mini-tabela ---
        total_vendas     = len(tabela_df)
        total_quantidade = int(tabela_df["QUANTIDADE"].fillna(0).sum())
        page_w, _ = A4
        usable_w = page_w - doc.leftMargin - doc.rightMargin
        kpi_data = [
            ["Total de Vendas Filtradas", f"{total_vendas:,}"],
            ["Quantidade Total",          f"{total_quantidade:,}"]
        ]
        kpi_table = Table(kpi_data, colWidths=[usable_w*0.5, usable_w*0.5])
        kpi_table.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), colors.whitesmoke),
            ("BACKGROUND",   (0, 1), (-1, 1), colors.lightgrey),
            ("TEXTCOLOR",    (0, 0), (-1, -1), colors.black),
            ("ALIGN",        (0, 0), (-1, -1), "LEFT"),
            ("FONTSIZE",     (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
            ("GRID",         (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        elems.append(kpi_table)
        elems.append(Spacer(1, 12))
    
        # --- Tabela principal ---
        main = tabela_df.copy()
        main["QUANTIDADE"] = main["QUANTIDADE"].fillna(0).astype(int)
        data = [main.columns.tolist()] + main.values.tolist()
        tab = Table(data, repeatRows=1, splitByRow=1)
        tab.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), colors.lightgrey),
            ("TEXTCOLOR",    (0, 0), (-1, 0), colors.black),
            ("ALIGN",        (0, 0), (-1, -1), "CENTER"),
            ("FONTSIZE",     (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING",(0, 0), (-1, 0), 6),
            ("GRID",         (0, 0), (-1, -1), 0.25, colors.grey),
        ]))
        elems.append(tab)
        elems.append(PageBreak())
    
        # --- Preparar resumos para o PDF ---
        def _prep_summary(df, label):
            df = df.copy()
            cols = df.columns.tolist()
            return df.rename(columns={
                cols[0]: label,
                "Quantidade_Unidades": "Quantidade",
                "Quantidade_de_Vendas": "Quantidade de Vendas"
            })
    
        df_h1_pdf   = _prep_summary(df_h1,   "Hierarquia 1")
        df_h2_pdf   = _prep_summary(df_h2,   "Hierarquia 2")
        df_tipo_pdf = _prep_summary(df_tipo, "Tipo de Envio")
    
        def resume(df, title):
            d = df.copy()
            for col in d.columns[1:]:
                d[col] = d[col].fillna(0).astype(int)
            data = [d.columns.tolist()] + d.values.tolist()
            t = Table(data, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                ("ALIGN",      (0,0), (-1,-1), "CENTER"),
                ("FONTSIZE",   (0,0), (-1,-1), 6),
                ("GRID",       (0,0), (-1,-1), 0.25, colors.grey),
            ]))
            return [Paragraph(title, styles["Heading3"]), Spacer(1,4), t]
    
        # --- Página de resumo: Hierarquia 1 ---
        elems.extend(resume(df_h1_pdf, "Hierarquia 1"))
        elems.append(PageBreak())
    
        # --- Página de resumo: Hierarquia 2 ---
        elems.extend(resume(df_h2_pdf, "Hierarquia 2"))
        elems.append(PageBreak())
    
        # --- Página de resumo: Tipo de Envio ---
        elems.extend(resume(df_tipo_pdf, "Tipo de Envio"))
        # (não precisa de PageBreak() final se for a última página)
    
        # --- Build e links ---
        doc.build(elems)
    
        pdf_b64 = base64.b64encode(buffer.getvalue()).decode()
        href_pdf = (
            f'<a style="margin-right:20px;" '
            f'href="data:application/pdf;base64,{pdf_b64}" '
            f'download="relatorio_expedicao.pdf">📄 Baixar PDF</a>'
        )
    
        xlsx_buf = BytesIO()
        with pd.ExcelWriter(xlsx_buf, engine="openpyxl") as w:
            main.to_excel(w, index=False, sheet_name="Dados")
            df_h1.to_excel(w, index=False, sheet_name="Hierarquia_1")
            df_h2.to_excel(w, index=False, sheet_name="Hierarquia_2")
            df_tipo.to_excel(w, index=False, sheet_name="Tipo_Envio")
        xlsx_b64 = base64.b64encode(xlsx_buf.getvalue()).decode()
        href_xlsx = (
            f'<a href="data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,{xlsx_b64}" '
            f'download="relatorio_expedicao.xlsx">⬇️ Baixar Excel</a>'
        )
    
        return href_pdf + href_xlsx

    # -- logo após os blocos de st.dataframe das 3 tabelas de resumo --
    periodo_venda     = (de_venda, ate_venda)
    periodo_expedicao = (de_limite, ate_limite)
    botoes = gerar_relatorio_pdf(
        tabela, df_h1, df_h2, df_tipo,
        periodo_venda, periodo_expedicao
    )
    st.markdown(botoes, unsafe_allow_html=True)


# ----------------- Fluxo Principal -----------------
if "code" in st.query_params:
    ml_callback()

df_vendas = carregar_vendas()

pagina = render_sidebar()
if pagina == "Dashboard":
    mostrar_dashboard()
elif pagina == "Contas Cadastradas":
    mostrar_contas_cadastradas()
elif pagina == "Relatórios":
    mostrar_relatorios()
elif pagina == "Expedição":
    mostrar_expedicao_logistica(df_vendas)
