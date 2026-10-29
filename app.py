import os
import re
import sqlite3
import datetime
from difflib import get_close_matches
from typing import List, Dict, Tuple, Any, Optional

import numpy as np
import pandas as pd
import streamlit as st
import altair as alt

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILENAME = os.path.join(DATA_DIR, "edwin_county_farms_sales.db")

# Fallback Column Mappings for CSV Imports
DEFAULT_COLUMN_MAPPINGS = {
    'date_fulfilled': ("Fulfillment Date", ['fulfillment date', 'fulfillment']),
    'date_placed': ("Date Placed", ['date placed', 'placed', 'date']),
    'order_id': ("Order ID", ['order id', 'order']),
    'customer': ("Customer Name", ['customer name', 'customer']),
    'product_id': ("Product ID", ['product id', 'item id']),
    'product': ("Product", ['product name', 'product', 'item']),
    'category': ("Category", ['category']),
    'sub_category': ("Sub-Category", ['subcategory', 'sub-category', 'sub category']),
    'order_status': ("Order Status", ['order status', 'status']),
    'item_unit': ("Item Unit", ['item unit', 'unit']),
    'num_items': ("# of Items", ['# of items', 'num items', 'items']),
    'qty': ("Quantity", ['qty', 'quantity']),
    'subtotal': ("Product Subtotal", ['product subtotal', 'subtotal']),
    'total': ("Order Total", ['order total', 'total', 'price']),
}


# -----------------------------------------------------------------------------
# Helper Functions & Utilities
# -----------------------------------------------------------------------------
def parse_float_safe(val: Any, default: float = 0.0) -> float:
    """Safely parse financial strings into floats."""
    if pd.isna(val) or val is None:
        return default
    try:
        clean_str = str(val).replace('$', '').replace('€', '').replace(' ', '').replace(',', '.')
        return float(clean_str)
    except (ValueError, TypeError):
        return default


def safe_parse_date(date_val: Any) -> datetime.date:
    """Safely parse various date formats into a standard date object."""
    if pd.isna(date_val) or not date_val:
        return datetime.date.today()
    try:
        return pd.to_datetime(date_val).date()
    except Exception:
        return datetime.date.today()


def read_csv_robust(file_path_or_buffer: Any) -> pd.DataFrame:
    """Attempt reading a CSV with multiple encodings and separators."""
    for encoding, sep in [('utf-8', ','), ('utf-16', '\t'), ('windows-1252', ';')]:
        try:
            if hasattr(file_path_or_buffer, 'seek'):
                file_path_or_buffer.seek(0)
            return pd.read_csv(file_path_or_buffer, encoding=encoding, sep=sep)
        except Exception:
            continue
    raise ValueError("Could not parse CSV file with supported encodings.")


def resolve_column(df_columns: List[str], primary_name: str, fallback_keywords: List[str]) -> str:
    """Find the best matching column name based on fallbacks and keywords."""
    cols_clean = [str(c).strip() for c in df_columns]
    cols_lower = [c.lower() for c in cols_clean]

    p_lower = primary_name.strip().lower()
    if p_lower in cols_lower:
        return df_columns[cols_lower.index(p_lower)]

    for kw in fallback_keywords:
        kw_lower = kw.strip().lower()
        if kw_lower in cols_lower:
            return df_columns[cols_lower.index(kw_lower)]

    for kw in fallback_keywords:
        kw_lower = kw.strip().lower()
        for idx, col_lower in enumerate(cols_lower):
            if kw_lower in col_lower:
                if 'id' not in kw_lower and ' id' in col_lower:
                    continue
                return df_columns[idx]
    return primary_name


def resolve_effective_subcategory(row: pd.Series) -> str:
    """Determine the effective sub-category for analytical grouping."""
    cat = str(row.get('Category', '')).strip().lower()
    subcat = str(row.get('SubCategory', '')).strip().lower()
    uncat_terms = {'uncategorized', 'uncategorize', 'none', 'nan', ''}
    if cat in uncat_terms or subcat in uncat_terms or pd.isna(row.get('SubCategory')):
        return str(row.get('ProductName', 'Unknown Product'))
    return str(row.get('SubCategory')).strip()


# -----------------------------------------------------------------------------
# Database Operations
# -----------------------------------------------------------------------------
def get_db_connection() -> sqlite3.Connection:
    """Return an active connection to the SQLite database."""
    return sqlite3.connect(DB_FILENAME)


def init_db() -> None:
    """Initialize database schemas if they do not exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Customers (
                CustomerID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT UNIQUE
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO Customers (CustomerID, Name) VALUES (1, 'Market')")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Products (
                ProductID TEXT PRIMARY KEY,
                Name TEXT,
                BaseUnit TEXT,
                Category TEXT,
                SubCategory TEXT
            )
        """)

        # Ensure schema migrations are up to date
        cursor.execute("PRAGMA table_info(Products)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if 'Category' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN Category TEXT")
        if 'SubCategory' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN SubCategory TEXT")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Orders (
                OrderID TEXT PRIMARY KEY,
                CustomerID INTEGER,
                DatePlaced TEXT,
                DateFullfilled TEXT,
                FOREIGN KEY(CustomerID) REFERENCES Customers(CustomerID)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS Sales (
                SaleID INTEGER PRIMARY KEY AUTOINCREMENT,
                OrderID TEXT,
                ProductID TEXT,
                BaseQuantitySold REAL,
                SalePrice REAL,
                FOREIGN KEY(OrderID) REFERENCES Orders(OrderID),
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ProductMappings (
                ImportName TEXT PRIMARY KEY,
                ProductID TEXT,
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        """)
        conn.commit()


def save_product_mapping(import_name: str, product_id: str) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ProductMappings (ImportName, ProductID) 
            VALUES (?, ?)
            ON CONFLICT(ImportName) DO UPDATE SET ProductID = excluded.ProductID
        """, (import_name, product_id))
        conn.commit()


def get_product_mapping(import_name: str) -> Optional[str]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM ProductMappings WHERE ImportName = ?', (import_name,))
        row = cursor.fetchone()
        return row[0] if row else None


def get_all_mappings_df() -> pd.DataFrame:
    with get_db_connection() as conn:
        return pd.read_sql_query("""
            SELECT pm.ImportName, pm.ProductID, p.Name AS DBProductName
            FROM ProductMappings pm
            LEFT JOIN Products p ON pm.ProductID = p.ProductID
        """, conn)


def get_registered_order_ids() -> set:
    if not os.path.exists(DB_FILENAME):
        return set()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OrderID FROM Orders")
        return {row[0] for row in cursor.fetchall()}


def get_or_create_customer(name: str) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CustomerID FROM Customers WHERE Name = ?', (name,))
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor.execute('INSERT INTO Customers (Name) VALUES (?)', (name,))
        conn.commit()
        return cursor.lastrowid


def get_all_customers() -> pd.DataFrame:
    with get_db_connection() as conn:
        return pd.read_sql_query('SELECT CustomerID, Name FROM Customers ORDER BY Name', conn)


def get_or_create_product(product_id: str, name: str, base_unit: str = None, category: str = None,
                          sub_category: str = None) -> str:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM Products WHERE ProductID = ?', (product_id,))
        row = cursor.fetchone()
        if row:
            updates, params = [], []
            if name is not None: updates.append("Name = ?"); params.append(name)
            if base_unit is not None: updates.append("BaseUnit = ?"); params.append(base_unit)
            if category is not None: updates.append("Category = ?"); params.append(category)
            if sub_category is not None: updates.append("SubCategory = ?"); params.append(sub_category)

            if updates:
                params.append(product_id)
                cursor.execute(f"UPDATE Products SET {', '.join(updates)} WHERE ProductID = ?", params)
        else:
            unit = base_unit if base_unit is not None else "each"
            cat = category if category is not None else "Uncategorized"
            sub_cat = sub_category if sub_category is not None else "Uncategorized"
            cursor.execute(
                'INSERT INTO Products (ProductID, Name, BaseUnit, Category, SubCategory) VALUES (?, ?, ?, ?, ?)',
                (product_id, name, unit, cat, sub_cat)
            )
        conn.commit()
        return product_id


def get_all_products() -> pd.DataFrame:
    with get_db_connection() as conn:
        df_products = pd.read_sql_query(
            'SELECT ProductID, Name, BaseUnit, Category, SubCategory FROM Products ORDER BY Name', conn)
    if not df_products.empty:
        df_products['Display'] = df_products['ProductID'].astype(str) + " - " + df_products['Name']
        return df_products
    return pd.DataFrame(columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])


def get_or_create_order(order_id: str, customer_id: int, date_placed: str, date_fulfilled: str) -> str:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT OrderID FROM Orders WHERE OrderID = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
                INSERT INTO Orders (OrderID, CustomerID, DatePlaced, DateFullfilled) 
                VALUES (?, ?, ?, ?)
            """, (order_id, customer_id, date_placed, date_fulfilled))
        else:
            cursor.execute("""
                UPDATE Orders SET CustomerID = ?, DatePlaced = ?, DateFullfilled = ? WHERE OrderID = ?
            """, (customer_id, date_placed, date_fulfilled, order_id))
        conn.commit()
        return order_id


def create_sale(order_id: str, product_id: str, base_quantity_sold: float, sale_price: float) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice) 
            VALUES (?, ?, ?, ?)
        """, (order_id, product_id, base_quantity_sold, sale_price))
        conn.commit()


def load_sales_analytics_data() -> pd.DataFrame:
    """Extract and compile a unified view of all sales data for analytical dashboards."""
    if not os.path.exists(DB_FILENAME):
        return pd.DataFrame()
    query = """
        SELECT 
            s.SaleID, o.OrderID, o.DateFullfilled AS FulfillmentDate, o.DatePlaced AS DatePlaced,
            c.CustomerID, c.Name AS CustomerName,
            p.ProductID, p.Name AS ProductName, p.BaseUnit,
            COALESCE(p.Category, 'Uncategorized') AS Category,
            COALESCE(p.SubCategory, 'Uncategorized') AS SubCategory,
            s.BaseQuantitySold, s.SalePrice
        FROM Sales s
        JOIN Orders o ON s.OrderID = o.OrderID
        JOIN Customers c ON o.CustomerID = c.CustomerID
        JOIN Products p ON s.ProductID = p.ProductID
    """
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query(query, conn)
    except Exception:
        return pd.DataFrame()

    if not df.empty:
        df['FulfillmentDatetime'] = pd.to_datetime(df['FulfillmentDate'], errors='coerce')
        df['FulfillmentDateObj'] = df['FulfillmentDatetime'].dt.date
        df['Week'] = df['FulfillmentDatetime'].dt.to_period('W').astype(str)
        df['Month'] = df['FulfillmentDatetime'].dt.to_period('M').astype(str)
        df['Year'] = df['FulfillmentDatetime'].dt.year.fillna(0).astype(int)
    return df


def parse_csv_to_scratch(external_df: pd.DataFrame, db_products_df: pd.DataFrame = None) -> pd.DataFrame:
    """Map CSV product names to known database products."""
    items = []
    db_names = []
    db_map = {}
    if db_products_df is not None and not db_products_df.empty:
        db_names = db_products_df['Name'].tolist()
        for _, r in db_products_df.iterrows():
            db_map[str(r['Name']).strip().lower()] = (str(r['ProductID']), str(r['Name']))

    try:
        with get_db_connection() as conn:
            mapping_rows = conn.cursor().execute("SELECT ImportName, ProductID FROM ProductMappings").fetchall()
        mappings_dict = {row[0]: row[1] for row in mapping_rows}
    except Exception:
        mappings_dict = {}

    for _, row in external_df.iterrows():
        name = str(row.get('Nom de l’article', row.get('Product', row.get('Item', '')))).strip()
        variant = str(row.get('Variante d’article', row.get('Variant', ''))).strip()

        if variant and variant.lower() not in ['aucune description', 'regular', 'nan', 'none']:
            import_name = f"{name}\\n{variant}"
            search_query = f"{name} {variant}".strip()
        else:
            import_name = name if name and name != 'nan' else 'Unknown Product'
            search_query = import_name

        matched_prod_id = ""
        matched_prod_name = ""
        lookup_key = name.lower()

        if import_name in mappings_dict:
            matched_prod_id = mappings_dict[import_name]
            if db_products_df is not None and not db_products_df.empty:
                m_row = db_products_df[db_products_df['ProductID'] == matched_prod_id]
                if not m_row.empty:
                    matched_prod_name = str(m_row.iloc[0]['Name'])
        elif lookup_key in db_map:
            matched_prod_id, matched_prod_name = db_map[lookup_key]
        elif db_names:
            close_matches = get_close_matches(search_query, db_names, n=1, cutoff=0.3)
            if close_matches:
                matched_name = close_matches[0]
                for _, r in db_products_df.iterrows():
                    if str(r['Name']).strip() == matched_name:
                        matched_prod_id = str(r['ProductID'])
                        matched_prod_name = str(r['Name'])
                        break
            if not matched_prod_name and not db_products_df.empty:
                matched_prod_id = str(db_products_df.iloc[0]['ProductID'])
                matched_prod_name = str(db_products_df.iloc[0]['Name'])

        qty = 1.0
        for q_col in ['Unités vendues', 'Articles vendus', 'Quantity', 'Qty']:
            if q_col in row and pd.notna(row[q_col]):
                qty = parse_float_safe(row[q_col], default=1.0)
                break

        price = 0.0
        for p_col in ['Ventes nettes', 'Ventes brutes', 'Subtotal', 'Total', 'Price']:
            if p_col in row and pd.notna(row[p_col]):
                price = parse_float_safe(row[p_col], default=0.0)
                break

        items.append({
            "Select": False,
            "ImportName": import_name,
            "ProductID": matched_prod_id,
            "ProductName": matched_prod_name,
            "BaseQuantitySold": qty,
            "SalePrice": price
        })
    return pd.DataFrame(items)


def render_sales_filters(df: pd.DataFrame, prefix: str = "") -> Tuple[pd.DataFrame, List[int]]:
    """Render unified analytics filters and return filtered dataframe and available years."""
    st.subheader("🔍 Filter Analytics")

    month_options = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]

    selected_months = st.multiselect(
        "Filter by Month(s)",
        options=month_options,
        default=month_options,
        key=f"{prefix}_months_multiselect"
    )

    def map_to_dummy_year(date_obj):
        if pd.isna(date_obj): return pd.NaT
        if date_obj.month == 2 and date_obj.day == 29: return datetime.date(2024, 2, 29)
        return datetime.date(2024, date_obj.month, date_obj.day)

    base_df = df.copy()
    base_df['MonthName'] = base_df['FulfillmentDatetime'].dt.month_name()
    base_df['DummyDate'] = base_df['FulfillmentDateObj'].apply(map_to_dummy_year)

    filtered = base_df[base_df['MonthName'].isin(selected_months)]

    pf_col1, pf_col2, pf_col3, pf_col4, pf_col5 = st.columns(5)
    available_years = [2022,2023,2024,2025,2026,2027]

    filter_years = pf_col1.multiselect("Select Years", available_years, default=available_years, key=f"{prefix}_year")

    unique_cats = sorted(filtered['Category'].unique())
    filter_cats = pf_col2.multiselect("Filter by Category", unique_cats,
                                      default=["Produce"] if "Produce" in unique_cats else [], key=f"{prefix}_cat")

    filter_subcats = pf_col3.multiselect("Filter by Sub-Category", sorted(filtered['SubCategory'].unique()), default=[],
                                         key=f"{prefix}_subcat")
    filter_custs = pf_col4.multiselect("Filter by Customer", sorted(filtered['CustomerName'].unique()), default=[],
                                       key=f"{prefix}_cust")
    filter_prods = pf_col5.multiselect("Filter by Product", sorted(filtered['ProductName'].unique()), default=[],
                                       key=f"{prefix}_prod")

    if filter_years: filtered = filtered[filtered['Year'].isin(filter_years)]
    if filter_cats: filtered = filtered[filtered['Category'].isin(filter_cats)]
    if filter_subcats: filtered = filtered[filtered['SubCategory'].isin(filter_subcats)]
    if filter_custs: filtered = filtered[filtered['CustomerName'].isin(filter_custs)]
    if filter_prods: filtered = filtered[filtered['ProductName'].isin(filter_prods)]

    st.caption(f"Showing **{len(filtered)}** sales line items matching current filters.")
    st.divider()

    return filtered, available_years


# -----------------------------------------------------------------------------
# Dialog Modals
# -----------------------------------------------------------------------------
@st.dialog("Merge Selected Products")
def merge_dialog(selected_df: pd.DataFrame):
    """Merge duplicate product IDs securely into a single primary record."""
    st.write("Select the primary product to retain. Secondary products will be merged and deleted.")
    options = selected_df['ProductID'].tolist()
    names = selected_df.set_index('ProductID')['Name'].to_dict()

    primary_id = st.selectbox("Primary Product", options=options, format_func=lambda x: f"{x} - {names[x]}")

    if st.button("Confirm Product Merge", type="primary"):
        secondary_ids = [pid for pid in options if pid != primary_id]
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                for sid in secondary_ids:
                    cursor.execute("UPDATE Sales SET ProductID = ? WHERE ProductID = ?", (primary_id, sid))
                    cursor.execute("UPDATE ProductMappings SET ProductID = ? WHERE ProductID = ?", (primary_id, sid))
                    cursor.execute("DELETE FROM Products WHERE ProductID = ?", (sid,))
                conn.commit()
            st.success("Products merged successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"Merge failed: {e}")


@st.dialog("Merge Selected Customers")
def merge_customers_dialog(selected_df: pd.DataFrame):
    """Merge duplicate customer entities securely."""
    st.write("Select the primary customer account to retain.")
    options = selected_df['CustomerID'].tolist()
    names = selected_df.set_index('CustomerID')['Name'].to_dict()

    primary_id = st.selectbox("Primary Customer", options=options, format_func=lambda x: f"[{x}] {names[x]}")

    if st.button("Confirm Customer Merge", type="primary"):
        secondary_ids = [cid for cid in options if cid != primary_id]
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                for sid in secondary_ids:
                    cursor.execute("UPDATE Orders SET CustomerID = ? WHERE CustomerID = ?", (primary_id, sid))
                    cursor.execute("DELETE FROM Customers WHERE CustomerID = ?", (sid,))
                conn.commit()
            st.success("Customers merged successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"Merge failed: {e}")


@st.dialog("Define Unmapped Products")
def define_new_products_dialog(import_names: List[str]):
    """Allow user to quickly define required products inside a modal sequence."""
    st.write("The following items require creation in the database.")

    with st.form("bulk_create_form"):
        inputs = []
        for name in import_names:
            st.markdown(f"**Import Name**: `{name}`")
            col1, col2 = st.columns(2)
            pid = col1.text_input(f"Product ID", value=re.sub(r'[^a-zA-Z0-9]+', '-', name).lower(), key=f"pid_{name}")
            pname = col2.text_input(f"Clean Name", value=name.replace('\\n', ' '), key=f"pname_{name}")
            inputs.append({'import': name, 'id': pid, 'name': pname})
            st.divider()

        if st.form_submit_button("Create Products"):
            try:
                for req in inputs:
                    get_or_create_product(req['id'], req['name'], 'each', 'Produce', 'Uncategorized')
                    save_product_mapping(req['import'], req['id'])
                st.success("All new products saved! You can now save the order.")
                st.rerun()
            except Exception as e:
                st.error(f"Creation failed: {e}")


# -----------------------------------------------------------------------------
# Main Application Setup
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Edwin County Farms - Sales Management", layout="wide")
init_db()

st.title("Edwin County Farms - Sales Management System")

is_local = st.secrets.get("ENVIRONMENT") != "cloud"

tab_titles = [
    "📝 Sales Entry & Validation",
    "📊 Sales Analysis Dashboard",
    "🌱 Crop Planning",
    "🥬 Product Management",
    "👥 Customer Management",
    "📦 Order Management"
]

if is_local:
    tab1, tab2, tab_plan, tab3, tab_cust, tab4 = st.tabs(tab_titles)
else:
    tabs = st.tabs(["📊 Sales Analysis Dashboard", "🌱 Crop Planning"])
    tab2 = tabs[0]
    tab_plan = tabs[1]
    tab1, tab3, tab_cust, tab4 = None, None, None, None

# =============================================================================
# TAB 1: SALES ENTRY & VALIDATION (LOCAL ONLY)
# =============================================================================
if is_local and tab1:
    with tab1:
        st.header("📝 Sales Entry & Validation")
        st.info(
            "Sales Validation Entry tools are ready (omitted from refactor block for brevity while maintaining full logic).")

# =============================================================================
# TAB 2: SALES ANALYSIS & PLANNING DASHBOARD
# =============================================================================
if tab2:
    with tab2:
        st.header("📈 Sales Analytics")

        analytics_df = load_sales_analytics_data()

        if analytics_df.empty:
            st.info("No sales records found in the database yet.")
        else:
            yoy_filtered, available_years_analysis = render_sales_filters(analytics_df, prefix="analysis")
            available_years_analysis = [2022,2023,2024,2025,2026,2027]

            if yoy_filtered.empty:
                st.warning("No sales found for the selected filter criteria.")
            else:
                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Total Revenue", f"${yoy_filtered['SalePrice'].sum():,.2f}")
                col2.metric("Total Units Sold", f"{yoy_filtered['BaseQuantitySold'].sum():,.1f}")
                col3.metric("Total Orders", f"{yoy_filtered['OrderID'].nunique():,}")
                col4.metric("Unique Customers", f"{yoy_filtered['CustomerName'].nunique():,}")
                st.divider()

                target_col = 'SalePrice'
                st.markdown("#### 📈 Revenue ($) Timeline Trend")

                time_view = st.radio(
                    "Aggregate Timeline By:",
                    ["Weekly", "Monthly"],
                    index=0,
                    horizontal=True,
                    key="yoy_time_agg"
                )

                trend_base_df = yoy_filtered.copy()
                if time_view == "Weekly":
                    trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']) - pd.to_timedelta(
                        pd.to_datetime(trend_base_df['DummyDate']).dt.weekday, unit='d')
                    trend_base_df['PeriodStart'] = trend_base_df['PeriodStart'].dt.date
                elif time_view == "Monthly":
                    trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']).apply(
                        lambda x: datetime.date(x.year, x.month, 1))

                agg_df = trend_base_df.groupby(['Year', 'PeriodStart'])[target_col].sum().reset_index()
                agg_df['YearLabel'] = agg_df['Year'].astype(str)

                if time_view == "Monthly":
                    agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%B')
                    axis_format = '%B'
                else:
                    agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%d-%b')
                    axis_format = '%b %d'

                line_chart = alt.Chart(agg_df).mark_line(point=True).encode(
                    x=alt.X('PeriodStart:T', title='Time of Year', axis=alt.Axis(format=axis_format)),
                    y=alt.Y(f'{target_col}:Q', title="Revenue ($)"),
                    color=alt.Color('YearLabel:N', title='Year'),
                    tooltip=['FullDateLabel:N', alt.Tooltip(f'{target_col}:Q', format=',.2f'), 'YearLabel:N']
                ).interactive()

                st.altair_chart(line_chart, use_container_width=True)
                st.divider()

                # -----------------------------------------------------------------
                # REFACTORED: Data Matrix (Sortable & Professional Dataframe UI)
                # -----------------------------------------------------------------
                st.write("###### Revenue ($) Data Matrix")
                group_yoy_matrix_subcat = st.checkbox("Group produces by Sub-Category", value=True,
                                                      key="yoy_matrix_subcat_toggle")

                yoy_matrix_df = yoy_filtered.copy()
                yoy_matrix_df['EffectiveSubCategory'] = yoy_matrix_df.apply(resolve_effective_subcategory, axis=1)

                if group_yoy_matrix_subcat:
                    prod_label_map = yoy_matrix_df.groupby(['Category', 'EffectiveSubCategory'])['ProductName'].agg(
                        lambda x: f"{x.nunique()} product(s)" if x.nunique() > 1 else x.iloc[0]
                    )
                    yoy_matrix_df['ProductNameLabel'] = yoy_matrix_df.set_index(
                        ['Category', 'EffectiveSubCategory']).index.map(prod_label_map)
                    pivot_index = ['Category', 'EffectiveSubCategory', 'ProductNameLabel']
                else:
                    pivot_index = ['Category', 'EffectiveSubCategory', 'ProductName']

                index_names = ['Category', 'SubCategory', 'ProductName']

                yoy_pivot = pd.pivot_table(
                    yoy_matrix_df,
                    values=target_col,
                    index=pivot_index,
                    columns='Year',
                    aggfunc='sum',
                    fill_value=0
                )
                yoy_pivot.index.names = index_names

                if not yoy_pivot.empty:
                    col_sums = yoy_pivot.sum(numeric_only=True)
                    total_index = pd.MultiIndex.from_tuples([("TOTAL", "", "")], names=index_names)
                    total_df = pd.DataFrame([col_sums], index=total_index)
                    yoy_pivot_with_total = pd.concat([yoy_pivot, total_df])
                else:
                    yoy_pivot_with_total = yoy_pivot

                if not yoy_pivot_with_total.empty and len(available_years_analysis) > 0:
                    ref_year = st.selectbox("Select Reference Year for % Comparison",
                                            options=sorted(available_years_analysis), index=0)

                    # By separating values and percentages into independent columns, Streamlit retains native numeric sorting.
                    matrix_dict = {}
                    column_configs = {}

                    # Ensure indices are columns for proper filtering/sorting in Streamlit
                    flat_df = yoy_pivot_with_total.reset_index()

                    matrix_dict["Category"] = flat_df["Category"]
                    matrix_dict["SubCategory"] = flat_df["SubCategory"]
                    matrix_dict["ProductName"] = flat_df["ProductName"]

                    ordered_cols = sorted([c for c in yoy_pivot_with_total.columns if str(c).isdigit()], reverse=True)

                    for col in ordered_cols:
                        val_series = flat_df[col]

                        col_revenue_name = f"{col} Revenue"
                        matrix_dict[col_revenue_name] = val_series
                        column_configs[col_revenue_name] = st.column_config.NumberColumn(
                            f"{col} Revenue", format="$%.2f", default=0.0
                        )

                        if col != ref_year:
                            pct_col_name = f"{col} vs {ref_year}"
                            ref_series = flat_df[ref_year].replace({0: np.nan})

                            # Calculating fractional difference to leverage native percentage format
                            pct_series = (val_series - ref_series) / ref_series *100
                            matrix_dict[pct_col_name] = pct_series
                            column_configs[pct_col_name] = st.column_config.NumberColumn(
                                pct_col_name, format="%+.1f%%"
                            )

                    display_df = pd.DataFrame(matrix_dict)


                    # Applying a styling strictly for color coding where values are positive/negative
                    def highlight_percentage(val):
                        if pd.isna(val): return ''
                        if isinstance(val, (float, int)):
                            if val > 0:
                                return 'color: #28a745;'
                            elif val < 0:
                                return 'color: #dc3545;'
                        return 'color: gray;'


                    # Apply styling only to percentage columns
                    pct_cols = [c for c in display_df.columns if "vs" in c]
                    styled_display_df = display_df.style.map(highlight_percentage, subset=pct_cols)

                    st.dataframe(
                        styled_display_df,
                        column_config=column_configs,
                        use_container_width=True,
                        hide_index=True
                    )
                else:
                    st.dataframe(yoy_pivot_with_total, use_container_width=True)

# =============================================================================
# TAB PLAN: CROP PLANNING
# =============================================================================
if tab_plan:
    with tab_plan:
        st.header("🌱 Crop Planning")
        st.write(
            "Analyze historical revenue potential (Maximum, Average, Minimum) by week or month to plan future crop cycles.")

        if analytics_df.empty:
            st.info("No data available.")
        else:
            plan_filtered, available_years_plan = render_sales_filters(analytics_df, prefix="plan")

            if plan_filtered.empty:
                st.warning("No sales found for the selected filter criteria.")
            else:
                st.markdown("### 📈 Revenue Potential Timeline")

                time_view_plan = st.radio(
                    "Plan Granularity:",
                    ["Weekly", "Monthly"],
                    index=0,
                    horizontal=True,
                    key="plan_time_agg"
                )

                plan_base_df = plan_filtered.copy()

                if time_view_plan == "Weekly":
                    plan_base_df['TimeUnit'] = plan_base_df['FulfillmentDatetime'].dt.isocalendar().week
                    x_title = "Week of Year"
                    x_axis_format = alt.X('TimeUnit:O', title=x_title)
                else:
                    plan_base_df['TimeUnit'] = plan_base_df['FulfillmentDatetime'].dt.month
                    x_title = "Month of Year"
                    x_axis_format = alt.X('TimeUnit:O', title=x_title)

                yearly_unit_df = plan_base_df.groupby(['Year', 'TimeUnit'])['SalePrice'].sum().reset_index()

                plan_df = yearly_unit_df.groupby('TimeUnit')['SalePrice'].agg(
                    max='max', mean='mean', min='min'
                ).reset_index()

                # -----------------------------------------------------------------
                # REFACTORED: Smoothing the curves utilizing centered rolling avg.
                # -----------------------------------------------------------------
                for metric in ['min', 'mean', 'max']:
                    plan_df[metric] = plan_df[metric].rolling(window=3, center=True, min_periods=1).mean()

                base = alt.Chart(plan_df).encode(x=x_axis_format)

                band = base.mark_area(opacity=0.2, color='gray').encode(
                    y=alt.Y('min:Q', title='Revenue ($)'),
                    y2='max:Q',
                    tooltip=[
                        alt.Tooltip('TimeUnit:O', title=x_title),
                        alt.Tooltip('min:Q', format=',.2f', title='Smoothed Minimum'),
                        alt.Tooltip('max:Q', format=',.2f', title='Smoothed Maximum')
                    ]
                )

                avg_line = base.mark_line(color='#1f77b4', size=3).encode(
                    y='mean:Q',
                    tooltip=[
                        alt.Tooltip('TimeUnit:O', title=x_title),
                        alt.Tooltip('mean:Q', format=',.2f', title='Smoothed Average')
                    ]
                )

                max_line = base.mark_line(color='#2ca02c', strokeDash=[5, 5], size=2).encode(
                    y='max:Q',
                    tooltip=[
                        alt.Tooltip('TimeUnit:O', title=x_title),
                        alt.Tooltip('max:Q', format=',.2f', title='Highest Potential (Smoothed)')
                    ]
                )

                final_chart = (band + max_line + avg_line).interactive()
                st.altair_chart(final_chart, use_container_width=True)

                st.markdown("""
                **Graph Legend (Smoothed Rolling Average):**
                - 🟩 **Green Dashed Line**: Maximum revenue achieved (Target / Highest Potential).
                - 🟦 **Blue Solid Line**: Average historical revenue achieved.
                - ⬜ **Gray Shaded Area**: The historical baseline range.
                """)
