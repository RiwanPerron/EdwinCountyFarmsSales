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
        uploaded_tab1_csv = st.file_uploader(
            "Upload Sales Export CSV for Validation",
            type=["csv"],
            key="tab1_csv_uploader"
        )

        df_raw = None
        if uploaded_tab1_csv is not None:
            local_csv_path = os.path.join(DATA_DIR, uploaded_tab1_csv.name)
            with open(local_csv_path, "wb") as f:
                f.write(uploaded_tab1_csv.getbuffer())

            st.success(f"File saved locally: `{local_csv_path}`")

            try:
                df_raw = read_csv_robust(local_csv_path)
            except Exception as e:
                st.error(f"Error loading CSV file: {e}")

        else:
            st.info("Please upload a CSV file above to review and validate pending sales orders.")

        if df_raw is not None:
            cols = list(df_raw.columns)

            # Resolve column mappings
            date_fulfilled_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['date_fulfilled'])
            date_placed_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['date_placed'])
            order_id_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['order_id'])
            customer_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['customer'])
            product_id_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['product_id'])
            product_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['product'])
            category_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['category'])
            sub_category_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['sub_category'])
            order_status_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['order_status'])
            item_unit_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['item_unit'])
            num_items_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['num_items'])
            qty_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['qty'])
            subtotal_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['subtotal'])
            total_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['total'])

            # Fill missing optional columns with sensible defaults to prevent KeyErrors
            defaults = {
                order_status_col: "Fulfilled",
                category_col: "Uncategorized",
                sub_category_col: "Uncategorized",
                item_unit_col: "each",
                num_items_col: 1.0,
                qty_col: 1.0,
                subtotal_col: 0.0,
                total_col: 0.0,
            }
            for col_name, default_val in defaults.items():
                if col_name not in df_raw.columns:
                    df_raw[col_name] = default_val

            registered_db_orders = get_registered_order_ids()
            valid_df = df_raw[~df_raw[order_status_col].astype(str).str.upper().str.contains('CANCEL', na=False)].copy()

            all_raw_dates = valid_df[date_fulfilled_col].dropna().unique()
            truly_pending_dates = []

            for d in all_raw_dates:
                df_d = valid_df[valid_df[date_fulfilled_col] == d]
                is_guest_d = df_d[customer_col].str.startswith("Guest customer", na=False)
                df_reg_d = df_d[~is_guest_d]
                df_gst_d = df_d[is_guest_d]

                reg_pending = not df_reg_d[order_id_col].astype(str).isin(registered_db_orders).all() if not df_reg_d.empty else False
                guest_pending = (not df_gst_d.empty) and (f"GUEST-{d}" not in registered_db_orders)

                if reg_pending or guest_pending:
                    truly_pending_dates.append(d)

            if len(truly_pending_dates) == 0:
                st.success("✨ All valid records have been registered!")
            else:
                db_products_df_bulk = get_all_products()
                valid_db_pids = set(db_products_df_bulk['ProductID'].astype(str).tolist()) if not db_products_df_bulk.empty else set()

                try:
                    with get_db_connection() as conn:
                        mapping_rows = conn.cursor().execute("SELECT ImportName, ProductID FROM ProductMappings").fetchall()
                    mappings_dict = {row[0]: row[1] for row in mapping_rows}
                except Exception:
                    mappings_dict = {}

                auto_ready_orders = []

                for d in truly_pending_dates:
                    df_date = valid_df[valid_df[date_fulfilled_col] == d]

                    is_guest = df_date[customer_col].str.startswith("Guest customer", na=False)
                    df_regular = df_date[~is_guest]
                    df_guests = df_date[is_guest]

                    guest_order_id = f"GUEST-{d}"
                    if not df_guests.empty and guest_order_id not in registered_db_orders:
                        grouped = df_guests.groupby([product_id_col, product_col, item_unit_col, category_col, sub_category_col])
                        guest_sales = []
                        all_guest_mapped = True

                        for (p_id, p_name, p_unit, p_cat, p_subcat), group in grouped:
                            csv_prod_name = str(p_name)
                            csv_prod_id = str(p_id)

                            mapped_pid = mappings_dict.get(csv_prod_name)
                            if mapped_pid and mapped_pid in valid_db_pids:
                                final_pid = mapped_pid
                            elif csv_prod_id in valid_db_pids:
                                final_pid = csv_prod_id
                            else:
                                all_guest_mapped = False
                                break

                            total_base_qty = 0.0
                            total_subtotal = 0.0
                            for _, row in group.iterrows():
                                ni = parse_float_safe(row.get(num_items_col), default=1.0)
                                q = parse_float_safe(row.get(qty_col), default=0.0)
                                total_base_qty += (ni * q)
                                total_subtotal += parse_float_safe(row.get(subtotal_col), default=0.0)

                            guest_sales.append({
                                'prod_id': final_pid,
                                'qty_sold': total_base_qty,
                                'sale_price': total_subtotal
                            })

                        if all_guest_mapped and guest_sales:
                            auto_ready_orders.append({
                                'order_id': guest_order_id,
                                'customer_name': "Market",
                                'date_placed': str(safe_parse_date(df_guests.iloc[0][date_placed_col])),
                                'date_fulfilled': str(safe_parse_date(d)),
                                'sales': guest_sales
                            })

                    df_regular_pending = df_regular[~df_regular[order_id_col].astype(str).isin(registered_db_orders)]
                    for o_id, o_df in df_regular_pending.groupby(order_id_col):
                        all_regular_mapped = True
                        regular_sales = []

                        for _, row in o_df.iterrows():
                            csv_prod_id = str(row[product_id_col])
                            csv_prod_name = str(row[product_col])

                            mapped_pid = mappings_dict.get(csv_prod_name)
                            if mapped_pid and mapped_pid in valid_db_pids:
                                final_pid = mapped_pid
                            elif csv_prod_id in valid_db_pids:
                                final_pid = csv_prod_id
                            else:
                                all_regular_mapped = False
                                break

                            ni = parse_float_safe(row.get(num_items_col), default=1.0)
                            q = parse_float_safe(row.get(qty_col), default=0.0)
                            calc_base_qty = ni * q
                            subtotal_val = parse_float_safe(row.get(subtotal_col), default=0.0)

                            regular_sales.append({
                                'prod_id': final_pid,
                                'qty_sold': calc_base_qty,
                                'sale_price': subtotal_val
                            })

                        if all_regular_mapped and regular_sales:
                            auto_ready_orders.append({
                                'order_id': str(o_id),
                                'customer_name': str(o_df.iloc[0][customer_col]),
                                'date_placed': str(safe_parse_date(o_df.iloc[0][date_placed_col])),
                                'date_fulfilled': str(safe_parse_date(o_df.iloc[0][date_fulfilled_col])),
                                'sales': regular_sales
                            })

                if len(auto_ready_orders) > 0:
                    st.info(f"✨ Found **{len(auto_ready_orders)}** order(s) where all products are natively mapped to the database.")
                    if st.button(f"🚀 Bulk Validate & Save Ready Orders ({len(auto_ready_orders)})", type="primary"):
                        with st.spinner("Saving mapped orders..."):
                            for o_data in auto_ready_orders:
                                c_id = get_or_create_customer(o_data['customer_name'])
                                get_or_create_order(o_data['order_id'], c_id, o_data['date_placed'], o_data['date_fulfilled'])
                                for sale in o_data['sales']:
                                    create_sale(o_data['order_id'], sale['prod_id'], sale['qty_sold'], sale['sale_price'])
                            st.success(f"Successfully processed {len(auto_ready_orders)} orders!")
                            st.rerun()
                    st.divider()

                try:
                    sorted_dates = sorted(truly_pending_dates, key=pd.to_datetime, reverse=True)
                except Exception:
                    sorted_dates = sorted(truly_pending_dates, reverse=True)

                selected_date = st.selectbox("1. Select Fulfillment Date to Review Manually", sorted_dates)
                df_date = valid_df[valid_df[date_fulfilled_col] == selected_date]

                is_guest = df_date[customer_col].str.startswith("Guest customer", na=False)
                df_regular = df_date[~is_guest]
                df_guests = df_date[is_guest]

                guest_order_id = f"GUEST-{selected_date}"
                market_already_registered = guest_order_id in registered_db_orders

                guest_aggregated = []
                if not df_guests.empty and not market_already_registered:
                    grouped = df_guests.groupby([product_id_col, product_col, item_unit_col, category_col, sub_category_col])
                    for (p_id, p_name, p_unit, p_cat, p_subcat), group in grouped:
                        total_base_qty = 0.0
                        total_subtotal = 0.0

                        for idx, row in group.iterrows():
                            ni = parse_float_safe(row.get(num_items_col), default=1.0)
                            q = parse_float_safe(row.get(qty_col), default=0.0)
                            total_base_qty += (ni * q)
                            total_subtotal += parse_float_safe(row.get(subtotal_col), default=0.0)

                        guest_aggregated.append({
                            'product_id': str(p_id),
                            'product_name': str(p_name),
                            'item_unit': str(p_unit) if pd.notna(p_unit) else "each",
                            'base_qty_sold': total_base_qty,
                            'sale_price': total_subtotal
                        })

                df_regular_pending = df_regular[~df_regular[order_id_col].astype(str).isin(registered_db_orders)]
                regular_order_ids = sorted(df_regular_pending[order_id_col].dropna().unique())

                guest_order_key = f"GUEST_BATCH_{selected_date}"

                dropdown_order_options = []
                if not df_guests.empty and not market_already_registered:
                    dropdown_order_options.append(guest_order_key)
                dropdown_order_options.extend(regular_order_ids)

                if len(dropdown_order_options) == 0:
                    st.info(f"All orders and market sales for {selected_date} have already been registered in the database.")
                else:
                    def format_order_display(o_id):
                        if o_id == guest_order_key:
                            total_guest_sum = sum([item['sale_price'] for item in guest_aggregated])
                            d_placed = df_guests.iloc[0][date_placed_col]
                            return f"🛒 [GUEST BATCH] Market | Total: ${total_guest_sum:.2f} | Placed: {d_placed}"
                        else:
                            o_df = df_regular_pending[df_regular_pending[order_id_col] == o_id]
                            c_name = o_df.iloc[0][customer_col]
                            d_placed = o_df.iloc[0][date_placed_col]
                            total_val = parse_float_safe(o_df.iloc[0][total_col], default=0.0)
                            return f"Order {o_id} | {c_name} | Total: ${total_val:.2f} | Placed: {d_placed}"

                    selected_order = st.selectbox("2. Select Order Number", dropdown_order_options, format_func=format_order_display)

                    if selected_order:
                        try:
                            db_products_df = get_all_products()
                            product_display_list = db_products_df['Display'].tolist() if not db_products_df.empty else []
                        except Exception:
                            product_display_list = []
                            db_products_df = pd.DataFrame(columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])

                        st.divider()
                        is_guest_selection = (selected_order == guest_order_key)

                        if is_guest_selection:
                            st.markdown(f"### 📦 Consolidated Guest Order: {selected_date}")
                            with st.container():
                                c1, c2, c3, c4 = st.columns(4)
                                customer_input = c1.text_input("Customer Name", value="Market")
                                order_input = c2.text_input("Order ID", value=guest_order_id)
                                date_p_input = c3.date_input("Date Placed", value=safe_parse_date(df_guests.iloc[0][date_placed_col]))
                                date_f_input = c4.date_input("Date Fulfilled", value=safe_parse_date(selected_date))
                        else:
                            df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                            st.markdown(f"### 📦 Order Details: {selected_order}")
                            first_row = df_order.iloc[0]
                            with st.container():
                                c1, c2, c3, c4 = st.columns(4)
                                customer_input = c1.text_input("Customer Name", value=str(first_row[customer_col]))
                                order_input = c2.text_input("Order ID", value=str(selected_order))
                                date_p_input = c3.date_input("Date Placed", value=safe_parse_date(first_row[date_placed_col]))
                                date_f_input = c4.date_input("Date Fulfilled", value=safe_parse_date(first_row[date_fulfilled_col]))

                        st.divider()
                        st.markdown("### 🛒 Sales within this Order")
                        sale_inputs = []

                        if is_guest_selection:
                            for idx, item in enumerate(guest_aggregated):
                                with st.container():
                                    csv_prod_id = item['product_id']
                                    csv_prod_name = item['product_name']
                                    csv_item_unit = item['item_unit']
                                    calc_base_qty = item['base_qty_sold']
                                    subtotal_val = item['sale_price']

                                    st.markdown(f"📌 **CSV Item:** `{csv_prod_name}`")

                                    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1.8, 1.2, 1.2, 0.8, 0.8, 0.8])

                                    dropdown_options = ["++ Create New Product ++"] + product_display_list
                                    mapped_pid = get_product_mapping(csv_prod_name)
                                    if mapped_pid:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == mapped_pid]
                                    else:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == csv_prod_id]

                                    default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                    selected_db_product = sc1.selectbox(f"Database Mapping [Guest {idx}]", dropdown_options, index=default_idx, key=f"g_dd_{idx}")

                                    if selected_db_product == "++ Create New Product ++":
                                        final_prod_id = sc2.text_input("New DB ID", value=csv_prod_id, key=f"g_pid_{idx}")
                                        final_prod_name = sc3.text_input("New DB Name", value=csv_prod_name, key=f"g_pn_{idx}")
                                        base_unit = sc4.text_input("Unit", value=csv_item_unit, key=f"g_bu_{idx}")
                                    else:
                                        selected_id = selected_db_product.split(" - ", 1)[0]
                                        selected_name = selected_db_product.split(" - ", 1)[1] if " - " in selected_db_product else ""
                                        matched_row = db_products_df[db_products_df['ProductID'].astype(str) == selected_id]
                                        db_unit_val = matched_row.iloc[0]['BaseUnit'] if not matched_row.empty else csv_item_unit

                                        final_prod_id, final_prod_name, base_unit = selected_id, selected_name, db_unit_val

                                        sc2.text_input("Matched DB ID", value=final_prod_id, disabled=True, key=f"g_pid_{idx}")
                                        sc3.text_input("Matched DB Name", value=final_prod_name, disabled=True, key=f"g_pn_{idx}")
                                        sc4.text_input("DB Unit", value=base_unit, disabled=True, key=f"g_bu_{idx}")

                                    qty_sold_input = sc5.number_input("Qty", value=float(calc_base_qty), step=0.1, key=f"g_qs_{idx}")
                                    sale_price_input = sc6.number_input("Price ($)", value=float(subtotal_val), step=1.0, key=f"g_sp_{idx}")

                                    sale_inputs.append({
                                        'original_name': csv_prod_name,
                                        'prod_id': final_prod_id,
                                        'prod_name': final_prod_name,
                                        'base_unit': base_unit,
                                        'category': None,
                                        'sub_category': None,
                                        'qty_sold': qty_sold_input,
                                        'sale_price': sale_price_input
                                    })
                                    st.divider()
                        else:
                            df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                            for idx, row in df_order.iterrows():
                                with st.container():
                                    csv_prod_id = str(row[product_id_col])
                                    csv_prod_name = str(row[product_col])
                                    csv_item_unit = str(row[item_unit_col]) if pd.notna(row[item_unit_col]) else "each"

                                    num_items = parse_float_safe(row.get(num_items_col), default=1.0)
                                    qty = parse_float_safe(row.get(qty_col), default=0.0)
                                    calc_base_qty = num_items * qty
                                    subtotal_val = parse_float_safe(row.get(subtotal_col), default=0.0)

                                    st.markdown(f"📌 **CSV Item:** `{csv_prod_name}`")

                                    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1.8, 1.2, 1.2, 0.8, 0.8, 0.8])

                                    dropdown_options = ["++ Create New Product ++"] + product_display_list
                                    mapped_pid = get_product_mapping(csv_prod_name)
                                    if mapped_pid:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == mapped_pid]
                                    else:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == csv_prod_id]

                                    default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                    selected_db_product = sc1.selectbox(f"Database Mapping [{idx}]", dropdown_options, index=default_idx, key=f"dd_{idx}")

                                    if selected_db_product == "++ Create New Product ++":
                                        final_prod_id = sc2.text_input("New DB ID", value=csv_prod_id, key=f"pid_{idx}")
                                        final_prod_name = sc3.text_input("New DB Name", value=csv_prod_name, key=f"pn_{idx}")
                                        base_unit = sc4.text_input("Unit", value=csv_item_unit, key=f"bu_{idx}")
                                    else:
                                        selected_id = selected_db_product.split(" - ", 1)[0]
                                        selected_name = selected_db_product.split(" - ", 1)[1] if " - " in selected_db_product else ""
                                        matched_row = db_products_df[db_products_df['ProductID'].astype(str) == selected_id]
                                        db_unit_val = matched_row.iloc[0]['BaseUnit'] if not matched_row.empty else csv_item_unit

                                        final_prod_id, final_prod_name, base_unit = selected_id, selected_name, db_unit_val

                                        sc2.text_input("Matched DB ID", value=final_prod_id, disabled=True, key=f"pid_{idx}")
                                        sc3.text_input("Matched DB Name", value=final_prod_name, disabled=True, key=f"pn_{idx}")
                                        sc4.text_input("DB Unit", value=base_unit, disabled=True, key=f"bu_{idx}")

                                    qty_sold_input = sc5.number_input("Qty", value=float(calc_base_qty), step=0.1, key=f"qs_{idx}")
                                    sale_price_input = sc6.number_input("Price ($)", value=float(subtotal_val), step=1.0, key=f"sp_{idx}")

                                    sale_inputs.append({
                                        'original_name': csv_prod_name,
                                        'prod_id': final_prod_id,
                                        'prod_name': final_prod_name,
                                        'base_unit': base_unit,
                                        'category': None,
                                        'sub_category': None,
                                        'qty_sold': qty_sold_input,
                                        'sale_price': sale_price_input
                                    })
                                    st.divider()

                        if st.button("✅ Validate & Save Order", type="primary", use_container_width=True):
                            try:
                                c_id = get_or_create_customer(customer_input)
                                o_id = get_or_create_order(order_input, c_id, str(date_p_input), str(date_f_input))
                                for sale in sale_inputs:
                                    p_id = get_or_create_product(sale['prod_id'], sale['prod_name'], sale['base_unit'], sale['category'], sale['sub_category'])
                                    create_sale(o_id, p_id, sale['qty_sold'], sale['sale_price'])
                                    if sale['original_name']:
                                        save_product_mapping(sale['original_name'], p_id)
                                st.success(f"Successfully processed Order {order_input} with {len(sale_inputs)} items!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving to database: {e}")

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

        if analytics_df.empty:
            st.info("No data available.")
        else:
            plan_filtered, available_years_plan = render_sales_filters(
                analytics_df, prefix="plan"
            )

            if plan_filtered.empty:
                st.warning("No sales found for the selected filter criteria.")
            else:
                st.markdown("### 📈 Revenue Potential Timeline")

                # Layout for time aggregation and reference year selection
                col_view, col_ref = st.columns(2)

                with col_view:
                    time_view_plan = st.radio(
                        "Time Aggregation",
                        ["Weekly", "Monthly"],
                        index=0,
                        horizontal=True,
                        key="plan_time_agg",
                        label_visibility="collapsed",
                    )

                plan_base_df = plan_filtered.copy()
                available_years_list = sorted(plan_base_df['Year'].dropna().unique().tolist(), reverse=True)

                with col_ref:
                    ref_year = st.selectbox(
                        "Select Reference Year for Comparison",
                        ["None"] + available_years_list
                    )

                # Set Time Unit
                if time_view_plan == "Weekly":
                    plan_base_df["TimeUnit"] = plan_base_df["FulfillmentDatetime"].dt.isocalendar().week
                    time_units_range = list(range(1, 54))  # Weeks 1-53
                    x_title = "Week of Year"
                    x_axis_format = alt.X("TimeUnit:O", title=x_title)
                else:
                    plan_base_df["TimeUnit"] = plan_base_df["FulfillmentDatetime"].dt.month
                    time_units_range = list(range(1, 13))  # Months 1-12
                    x_title = "Month of Year"
                    x_axis_format = alt.X("TimeUnit:O", title=x_title)

                # --- 1. DETERMINE ROWS: First not null between Sub-Category or Product Name ---
                subcat_col = 'SubCategory' if 'SubCategory' in plan_base_df.columns else (
                    'Sub-Category' if 'Sub-Category' in plan_base_df.columns else None)
                prod_col = 'Product Name' if 'Product Name' in plan_base_df.columns else (
                    'Product' if 'Product' in plan_base_df.columns else (
                        'Item' if 'Item' in plan_base_df.columns else None))

                if subcat_col and prod_col:
                    plan_base_df['ItemLevel'] = plan_base_df[subcat_col].fillna(plan_base_df[prod_col])
                elif prod_col:
                    plan_base_df['ItemLevel'] = plan_base_df[prod_col]
                elif subcat_col:
                    plan_base_df['ItemLevel'] = plan_base_df[subcat_col]
                else:
                    plan_base_df['ItemLevel'] = 'All'

                # --- 2. GENERATE A MATRIX FOR EACH YEAR ---
                # Rows = ItemLevel, Columns = TimeUnit, Cells = Sum of Sales
                years = plan_base_df['Year'].dropna().unique()
                items = plan_base_df['ItemLevel'].dropna().unique()

                raw_grouped = plan_base_df.groupby(['Year', 'ItemLevel', 'TimeUnit'])['SalePrice'].sum().reset_index()

                yearly_matrices = {}
                for y in years:
                    df_y = raw_grouped[raw_grouped['Year'] == y]
                    mat = df_y.pivot(index='ItemLevel', columns='TimeUnit', values='SalePrice')
                    # Reindex ensures identical rows and columns for all matrices (fills missing weeks/items with 0)
                    mat = mat.reindex(index=items, columns=time_units_range).fillna(0)
                    yearly_matrices[y] = mat

                # --- 3. CREATE MAX AND AVERAGE MATRICES ---
                if yearly_matrices:
                    # Concatenate all yearly matrices to easily aggregate cell-by-cell
                    concat_matrices = pd.concat(yearly_matrices.values(), keys=yearly_matrices.keys(), names=['Year'])

                    # Group by the ItemLevel to compress the years down into 2 master matrices
                    matrix_max = concat_matrices.groupby('ItemLevel').max()
                    matrix_avg = concat_matrices.groupby('ItemLevel').mean()
                else:
                    matrix_max = pd.DataFrame(0, index=items, columns=time_units_range)
                    matrix_avg = pd.DataFrame(0, index=items, columns=time_units_range)

                # --- 4. COMPUTE THE TIMELINE FROM THE MATRICES ---
                # Sum the columns (all filtered items) to get the weekly/monthly timeline
                timeline_max = matrix_max.sum(axis=0)
                timeline_avg = matrix_avg.sum(axis=0)

                plan_df = pd.DataFrame({
                    'TimeUnit': time_units_range,
                    'max_raw': timeline_max.values,
                    'mean_raw': timeline_avg.values
                })

                if ref_year != "None":
                    if ref_year in yearly_matrices:
                        timeline_ref = yearly_matrices[ref_year].sum(axis=0)
                        plan_df['ref_raw'] = timeline_ref.values
                    else:
                        plan_df['ref_raw'] = 0

                # --- 5. APPLY SMOOTHING FOR DISPLAY AND METRICS ---
                plan_df["max_smooth"] = plan_df["max_raw"].rolling(window=3, center=True, min_periods=1).mean()
                plan_df["mean_smooth"] = plan_df["mean_raw"].rolling(window=3, center=True, min_periods=1).mean()

                if ref_year != "None":
                    plan_df["ref_smooth"] = plan_df["ref_raw"].rolling(window=3, center=True, min_periods=1).mean()

                # Calculate metrics strictly from the smoothed curves
                total_smoothed_max = plan_df["max_smooth"].sum()
                total_smoothed_avg = plan_df["mean_smooth"].sum()
                if ref_year != "None":
                    total_smoothed_ref = plan_df["ref_smooth"].sum()

                # --- BUILD ALTAIR CHART ---
                base = alt.Chart(plan_df).encode(x=x_axis_format)

                avg_line = base.mark_line(color="#1f77b4", size=3).encode(
                    y=alt.Y("mean_smooth:Q", title="Revenue ($)"),
                    tooltip=[
                        alt.Tooltip("TimeUnit:O", title=x_title),
                        alt.Tooltip("mean_smooth:Q", format=",.2f", title="Smoothed Average"),
                    ],
                )

                max_line = base.mark_line(color="#2ca02c", strokeDash=[5, 5], size=2).encode(
                    y="max_smooth:Q",
                    tooltip=[
                        alt.Tooltip("TimeUnit:O", title=x_title),
                        alt.Tooltip("max_smooth:Q", format=",.2f", title="Highest Potential (Smoothed)"),
                    ],
                )

                chart_layers = [max_line, avg_line]

                if ref_year != "None":
                    ref_line = base.mark_line(color="#ff7f0e", size=2).encode(
                        y="ref_smooth:Q",
                        tooltip=[
                            alt.Tooltip("TimeUnit:O", title=x_title),
                            alt.Tooltip("ref_smooth:Q", format=",.2f", title=f"{ref_year} Sales (Smoothed)"),
                        ],
                    )
                    chart_layers.append(ref_line)

                final_chart = alt.layer(*chart_layers).interactive()
                st.altair_chart(final_chart, use_container_width=True)

                # --- DISPLAY EXACT TOTAL METRICS ---
                if ref_year != "None":
                    m1, m2, m3 = st.columns(3)
                    m1.metric(
                        label=f"{ref_year} Total Sales (Reference)",
                        value=f"${total_smoothed_ref:,.2f}"
                    )
                    m2.metric(
                        label="Historical Average Target",
                        value=f"${total_smoothed_avg:,.2f}",
                        delta=f"{((total_smoothed_avg - total_smoothed_ref) / total_smoothed_ref * 100):.1f}% vs {ref_year}" if total_smoothed_ref > 0 else None
                    )
                    m3.metric(
                        label="Maximum Potential Target",
                        value=f"${total_smoothed_max:,.2f}",
                        delta=f"{((total_smoothed_max - total_smoothed_ref) / total_smoothed_ref * 100):.1f}% vs {ref_year}" if total_smoothed_ref > 0 else None
                    )

                    st.markdown(f"""
                    **Graph Legend (Smoothed 3-Period Rolling Average):**
- 🟩 **Green Dashed Line**: Maximum revenue capacity (Sum of best years per item).
- 🟦 **Blue Solid Line**: Average revenue capacity (Sum of average years per item).
- 🟧 **Orange Solid Line**: Actual performance in {ref_year}.
                    """)
                else:
                    m1, m2 = st.columns(2)
                    m1.metric(
                        label="Historical Average Target",
                        value=f"${total_smoothed_avg:,.2f}",
                    )
                    m2.metric(
                        label="Maximum Potential Target",
                        value=f"${total_smoothed_max:,.2f}",
                        delta=f"+${(total_smoothed_max - total_smoothed_avg):,.2f} potential upside",
                    )

                    st.markdown("""
                    **Graph Legend (Smoothed 3-Period Rolling Average):**
- 🟩 **Green Dashed Line**: Maximum revenue capacity (Sum of best years per item).
- 🟦 **Blue Solid Line**: Average revenue capacity (Sum of average years per item).
                    """)

# =============================================================================
# TAB 3: PRODUCT MANAGEMENT (LOCAL ONLY)
# =============================================================================
if is_local and tab3:
    with tab3:
        st.header("🥬 Product Management")

        subtab3_1, subtab3_2 = st.tabs(["🥬 Product Catalog", "🔗 Product Mappings Table"])

        # -------------------------------------------------------------------------
        # SUB-TAB 3.1: PRODUCT CATALOG
        # -------------------------------------------------------------------------
        with subtab3_1:
            st.write("View, create, edit, and merge product characteristics stored in the database.")

            with st.expander("➕ Add New Product", expanded=False):
                with st.form("add_product_form"):
                    ap_col1, ap_col2, ap_col3, ap_col4, ap_col5 = st.columns(5)
                    new_p_id = ap_col1.text_input("Product ID (e.g. carrots-baby)")
                    new_p_name = ap_col2.text_input("Product Name (e.g. Carrots Baby)")
                    new_p_unit = ap_col3.text_input("Base Unit", value="each")
                    new_p_cat = ap_col4.text_input("Category", value="Produce")
                    new_p_subcat = ap_col5.text_input("Sub-Category", value="Uncategorized")

                    submit_new_prod = st.form_submit_button("💾 Save New Product")
                    if submit_new_prod:
                        if not new_p_id.strip() or not new_p_name.strip():
                            st.error("Please provide both a Product ID and Product Name.")
                        else:
                            try:
                                get_or_create_product(new_p_id.strip(), new_p_name.strip(), new_p_unit.strip(),
                                                      new_p_cat.strip(), new_p_subcat.strip())
                                st.success(f"Product '{new_p_name}' created successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error creating product: {e}")

            st.divider()

            with get_db_connection() as conn:
                prod_df = pd.read_sql_query("SELECT ProductID, Name, BaseUnit, Category, SubCategory FROM Products ORDER BY Name", conn)

            if prod_df.empty:
                st.info("No products found in the database yet. Process sales in Tab 1 or add products above.")
            else:
                prod_df.insert(0, "Select for Merge", False)

                st.write("Edit the table below directly and click **Save Product Changes** when finished, OR select products to **Merge**.")

                c_merge_btn, _ = st.columns([1, 2])

                edited_prod_df = st.data_editor(
                    prod_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="product_editor",
                    column_config={
                        "Select for Merge": st.column_config.CheckboxColumn("Merge", default=False),
                        "ProductID": st.column_config.TextColumn("Product ID", disabled=True),
                        "Name": st.column_config.TextColumn("Product Name"),
                        "BaseUnit": st.column_config.TextColumn("Base Unit (e.g. pound, bunch)"),
                        "Category": st.column_config.TextColumn("Category"),
                        "SubCategory": st.column_config.TextColumn("Sub-Category")
                    }
                )

                selected_for_merge = edited_prod_df[edited_prod_df["Select for Merge"] == True]

                with c_merge_btn:
                    if st.button("🔗 Merge Selected Products", type="secondary"):
                        if len(selected_for_merge) >= 2:
                            merge_dialog(selected_for_merge)
                        else:
                            st.error("Please check the 'Merge' box on at least 2 products before clicking.")

                st.write("---")
                if st.button("💾 Save Product Info Changes", type="primary"):
                    try:
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            for _, row in edited_prod_df.iterrows():
                                if pd.notna(row['ProductID']) and str(row['ProductID']).strip():
                                    cursor.execute('''
                                        UPDATE Products 
                                        SET Name = ?, BaseUnit = ?, Category = ?, SubCategory = ? 
                                        WHERE ProductID = ?
                                    ''', (row['Name'], row['BaseUnit'], row['Category'], row['SubCategory'], row['ProductID']))
                            conn.commit()
                        st.success("Product characteristics updated successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error updating products: {e}")

        # -------------------------------------------------------------------------
        # SUB-TAB 3.2: PRODUCT MAPPINGS TABLE
        # -------------------------------------------------------------------------
        with subtab3_2:
            st.write("Manage the single shared mapping table used across all CSV imports (Tab 1 & Tab 5).")

            mappings_df = get_all_mappings_df()

            if mappings_df.empty:
                st.info("No mappings saved yet. Map products in the Sales Entry or Market Importer tab to save them automatically.")
            else:
                db_prods = get_all_products()
                db_prod_options = db_prods['ProductID'].tolist() if not db_prods.empty else []

                edited_mappings_df_sub = st.data_editor(
                    mappings_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="tab3_mappings_editor",
                    column_config={
                        "ImportName": st.column_config.TextColumn("CSV Product Name (ImportName)", required=True),
                        "ProductID": st.column_config.SelectboxColumn("Mapped DB ProductID", options=db_prod_options, required=True),
                        "DBProductName": st.column_config.TextColumn("Mapped DB Product Name", disabled=True),
                    }
                )

                if st.button("💾 Save Sub-Tab Mapping Changes", type="primary", key="save_tab3_mappings"):
                    try:
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM ProductMappings")
                            for _, row in edited_mappings_df_sub.iterrows():
                                if pd.notna(row['ImportName']) and str(row['ImportName']).strip() != "":
                                    cursor.execute('''
                                        INSERT INTO ProductMappings (ImportName, ProductID) 
                                        VALUES (?, ?)
                                    ''', (str(row['ImportName']).strip(), str(row['ProductID']).strip()))
                            conn.commit()

                        st.success("Mappings updated successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error updating mappings: {e}")


# =============================================================================
# TAB 4: CUSTOMER MANAGEMENT (LOCAL ONLY)
# =============================================================================
if is_local and tab_cust:
    with tab_cust:
        st.header("👥 Customer Management")
        st.write("View, create, edit, and merge customer records stored in the database.")

        with st.expander("➕ Add New Customer", expanded=False):
            with st.form("add_customer_form"):
                new_c_name = st.text_input("Customer Name (e.g. Restaurant Le Bistro)")
                submit_new_cust = st.form_submit_button("💾 Save New Customer")
                if submit_new_cust:
                    if not new_c_name.strip():
                        st.error("Please provide a valid Customer Name.")
                    else:
                        try:
                            get_or_create_customer(new_c_name.strip())
                            st.success(f"Customer '{new_c_name.strip()}' created successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error creating customer: {e}")

        st.divider()

        with get_db_connection() as conn:
            cust_df = pd.read_sql_query("SELECT CustomerID, Name FROM Customers ORDER BY Name", conn)

        if cust_df.empty:
            st.info("No customers found in the database yet.")
        else:
            cust_df.insert(0, "Select for Merge", False)

            st.write("Edit customer names directly in the table below and click **Save Customer Changes**, OR select customers to **Merge**.")

            c_merge_btn, _ = st.columns([1, 2])

            edited_cust_df = st.data_editor(
                cust_df,
                num_rows="dynamic",
                use_container_width=True,
                key="customer_editor",
                column_config={
                    "Select for Merge": st.column_config.CheckboxColumn("Merge", default=False),
                    "CustomerID": st.column_config.NumberColumn("Customer ID", disabled=True),
                    "Name": st.column_config.TextColumn("Customer Name", required=True)
                }
            )

            selected_cust_for_merge = edited_cust_df[edited_cust_df["Select for Merge"] == True]

            with c_merge_btn:
                if st.button("🔗 Merge Selected Customers", type="secondary", key="btn_merge_cust"):
                    if len(selected_cust_for_merge) >= 2:
                        merge_customers_dialog(selected_cust_for_merge)
                    else:
                        st.error("Please check the 'Merge' box on at least 2 customers before clicking.")

            st.write("---")
            if st.button("💾 Save Customer Info Changes", type="primary", key="btn_save_cust"):
                try:
                    with get_db_connection() as conn:
                        cursor = conn.cursor()
                        for _, row in edited_cust_df.iterrows():
                            if pd.notna(row['CustomerID']) and str(row['Name']).strip():
                                cursor.execute('''
                                    UPDATE Customers 
                                    SET Name = ? 
                                    WHERE CustomerID = ?
                                ''', (str(row['Name']).strip(), int(row['CustomerID'])))
                        conn.commit()
                    st.success("Customer names updated successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error updating customers: {e}")

with tab4:
    st.header("📦 Order Management")

    mode = st.radio(
        "Select Action:",
        ["📥 Import Market Order from CSV", "✏️ Search & Edit Existing Orders"],
        horizontal=True,
        key="tab4_action_mode"
    )

    st.divider()

    # -------------------------------------------------------------------------
    # MODE 1: IMPORT MARKET ORDER FROM CSV
    # -------------------------------------------------------------------------
    if mode == "📥 Import Market Order from CSV":
        st.subheader("📥 Import Market Sales CSV")
        st.write(
            "Upload a market sales CSV to automatically prefill dates, map products, aggregate duplicates, and register a new order.")

        try:
            db_products_df = get_all_products()
            product_display_list = ["++ Create New Product ++"] + (
                db_products_df['Display'].tolist() if not db_products_df.empty else [])
            display_to_id = {row['Display']: row['ProductID'] for _, row in
                             db_products_df.iterrows()} if not db_products_df.empty else {}
            id_to_display = {row['ProductID']: row['Display'] for _, row in
                             db_products_df.iterrows()} if not db_products_df.empty else {}
        except Exception:
            db_products_df = pd.DataFrame()
            product_display_list = ["++ Create New Product ++"]
            display_to_id, id_to_display = {}, {}

        if len(product_display_list) <= 1:
            st.info(
                "💡 Your database has no existing products. Unmapped items will automatically default to creating a new product.")

        uploaded_file = st.file_uploader("Upload market POS CSV", type=["csv"],
                                         key="market_csv_upload")

        extracted_date = datetime.date.today()
        if uploaded_file:
            date_matches = re.findall(r'(\d{4}-\d{2}-\d{2})', uploaded_file.name)
            if date_matches:
                try:
                    extracted_date = datetime.datetime.strptime(date_matches[0], "%Y-%m-%d").date()
                except ValueError:
                    pass

            if st.session_state.get("market_csv_name") != uploaded_file.name:
                st.session_state["market_date_placed"] = extracted_date
                st.session_state["market_date_fulfilled"] = extracted_date
                st.session_state["market_order_id"] = f"MARKET-{extracted_date.strftime('%Y%m%d')}"

        with st.container():
            c1, c2, c3, c4 = st.columns(4)
            default_order_id = f"MARKET-{extracted_date.strftime('%Y%m%d')}"
            new_order_id = c1.text_input("Order ID", value=default_order_id, key="market_order_id")
            new_customer = c2.text_input("Customer Name", value="Market",
                                         key="market_customer_name")
            new_date_placed = c3.date_input("Date Placed", value=extracted_date,
                                            key="market_date_placed")
            new_date_fulfilled = c4.date_input("Date Fulfilled", value=extracted_date,
                                               key="market_date_fulfilled")

        if uploaded_file and product_display_list:
            if "market_csv_name" not in st.session_state or st.session_state[
                "market_csv_name"] != uploaded_file.name:
                try:
                    uploaded_file.seek(0)
                    external_df = pd.read_csv(uploaded_file)
                except Exception:
                    uploaded_file.seek(0)
                    try:
                        external_df = pd.read_csv(uploaded_file, encoding='utf-16', sep='\t')
                    except Exception:
                        uploaded_file.seek(0)
                        external_df = pd.read_csv(uploaded_file, encoding='windows-1252', sep=';')

                parsed_df = parse_csv_to_scratch(external_df, db_products_df)

                parsed_df['Selected DB Product'] = parsed_df['ProductID'].apply(
                    lambda x: id_to_display.get(x, "++ Create New Product ++"))

                st.session_state["market_draft_df"] = parsed_df[
                    ['ImportName', 'Selected DB Product', 'BaseQuantitySold', 'SalePrice']]
                st.session_state["market_csv_name"] = uploaded_file.name

            st.subheader("Review & Map Products")
            st.info(
                "Multiple rows mapped to the same product will be automatically merged into a single sales line upon saving.")

            edited_market_df = st.data_editor(
                st.session_state["market_draft_df"],
                num_rows="dynamic",
                use_container_width=True,
                key="market_items_editor",
                column_config={
                    "ImportName": st.column_config.TextColumn("CSV Original Name", disabled=True),
                    "Selected DB Product": st.column_config.SelectboxColumn("Mapped DB Product",
                                                                            options=product_display_list,
                                                                            required=True),
                    "BaseQuantitySold": st.column_config.NumberColumn("Quantity Sold", step=0.1),
                    "SalePrice": st.column_config.NumberColumn("Sale Price ($)", step=1.0)
                }
            )

            st.divider()

            if st.button("🚀 Aggregate & Save Market Order", type="primary"):
                if not new_order_id.strip():
                    st.error("Please enter a valid Order ID.")
                elif edited_market_df["Selected DB Product"].isna().any() or edited_market_df[
                    "Selected DB Product"].eq(
                        "").any():
                    st.error("Please ensure all items are mapped to a Database Product.")
                else:
                    needs_creation = edited_market_df[
                        edited_market_df["Selected DB Product"] == "++ Create New Product ++"]

                    if not needs_creation.empty:
                        import_names_to_create = needs_creation["ImportName"].unique().tolist()
                        define_new_products_dialog(import_names_to_create)
                    else:
                        with st.spinner("Saving mappings and aggregating order items..."):
                            edited_market_df['FinalProductID'] = edited_market_df[
                                'Selected DB Product'].map(
                                display_to_id)

                            for _, row in edited_market_df.iterrows():
                                csv_name = row['ImportName']
                                p_id = row['FinalProductID']
                                if pd.notna(csv_name) and str(csv_name).strip() != "" and pd.notna(
                                        p_id):
                                    save_product_mapping(str(csv_name).strip(), str(p_id))

                            aggregated_df = edited_market_df.groupby('FinalProductID').agg({
                                'BaseQuantitySold': 'sum',
                                'SalePrice': 'sum'
                            }).reset_index()

                            try:
                                c_id = get_or_create_customer(new_customer)
                                get_or_create_order(new_order_id, c_id, str(new_date_placed),
                                                    str(new_date_fulfilled))

                                for _, row in aggregated_df.iterrows():
                                    create_sale(new_order_id, row['FinalProductID'],
                                                float(row['BaseQuantitySold']),
                                                float(row['SalePrice']))

                                st.success(
                                    f"🎉 Successfully created order `{new_order_id}` with {len(aggregated_df)} combined line items!")

                                del st.session_state["market_draft_df"]
                                del st.session_state["market_csv_name"]

                                if st.button("Return to Order Manager"):
                                    st.rerun()

                            except Exception as e:
                                st.error(f"Database error while saving order: {e}")

    # -------------------------------------------------------------------------
    # MODE 2: SEARCH & EDIT EXISTING ORDERS
    # -------------------------------------------------------------------------
    else:
        st.subheader("✏️ Search & Edit Existing Registered Orders")

        conn = sqlite3.connect(DB_FILENAME)
        registered_orders_df = pd.read_sql_query('''
                        SELECT o.OrderID, c.Name AS CustomerName, o.DatePlaced, o.DateFullfilled 
                        FROM Orders o
                        JOIN Customers c ON o.CustomerID = c.CustomerID
                        ORDER BY o.DateFullfilled DESC
                    ''', conn)

        registered_orders_df['DateFullfilled_Obj'] = pd.to_datetime(
            registered_orders_df['DateFullfilled'],
            errors='coerce',
            format='mixed'
        ).dt.date
        registered_orders_df['DateFullfilled_Obj'] = registered_orders_df[
            'DateFullfilled_Obj'].fillna(
            registered_orders_df['DateFullfilled']
        )

        order_products_df = pd.read_sql_query('''
                        SELECT s.OrderID, p.Name AS ProductName
                        FROM Sales s
                        JOIN Products p ON s.ProductID = p.ProductID
                    ''', conn)
        conn.close()

        if registered_orders_df.empty:
            st.info("No orders registered in the database yet.")
        else:
            st.markdown("### 🔍 Search & Filter Orders")

            f_col1, f_col2, f_col3 = st.columns(3)

            all_order_dates = sorted(
                registered_orders_df['DateFullfilled_Obj'].dropna().unique().tolist(),
                reverse=True, key=str)
            search_dates = f_col1.multiselect("Filter by Date Fulfilled", all_order_dates,
                                              default=[],
                                              key="search_edit_date")

            all_sold_products = sorted(order_products_df['ProductName'].dropna().unique().tolist())
            search_products = f_col2.multiselect("Filter by Product Contained", all_sold_products,
                                                 default=[],
                                                 key="search_edit_prod")

            all_order_customers = sorted(
                registered_orders_df['CustomerName'].dropna().unique().tolist())
            search_customers = f_col3.multiselect("Filter by Customer", all_order_customers,
                                                  default=[],
                                                  key="search_edit_cust")

            valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_dates:
                registered_orders_df = registered_orders_df[
                    registered_orders_df['DateFullfilled_Obj'].isin(search_dates)]
                valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_customers:
                registered_orders_df = registered_orders_df[
                    registered_orders_df['CustomerName'].isin(search_customers)]
                valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_products:
                matched_orders = \
                order_products_df[order_products_df['ProductName'].isin(search_products)][
                    'OrderID'].unique()
                valid_order_ids = [oid for oid in valid_order_ids if oid in matched_orders]
                registered_orders_df = registered_orders_df[
                    registered_orders_df['OrderID'].isin(valid_order_ids)]

            st.divider()

            if not valid_order_ids:
                st.warning("No orders match the selected filters.")
            else:
                def format_reg_order(o_id):
                    row = registered_orders_df[registered_orders_df['OrderID'] == o_id].iloc[0]
                    return f"Order {o_id} | {row['CustomerName']} | Fulfilled: {row['DateFullfilled']}"


                selected_reg_order = st.selectbox("Select Registered Order to Edit",
                                                  valid_order_ids,
                                                  format_func=format_reg_order)

                if selected_reg_order:
                    conn = sqlite3.connect(DB_FILENAME)
                    order_header = pd.read_sql_query('SELECT * FROM Orders WHERE OrderID = ?', conn,
                                                     params=(selected_reg_order,)).iloc[0]
                    sales_items_df = pd.read_sql_query('''
                                    SELECT s.SaleID, s.ProductID, p.Name AS ProductName, s.BaseQuantitySold, s.SalePrice
                                    FROM Sales s
                                    JOIN Products p ON s.ProductID = p.ProductID
                                    WHERE s.OrderID = ?
                                ''', conn, params=(selected_reg_order,))
                    conn.close()

                    all_cust_df = get_all_customers()
                    cust_names = all_cust_df['Name'].tolist()
                    current_cust_row = all_cust_df[
                        all_cust_df['CustomerID'] == order_header['CustomerID']]
                    current_cust_name = current_cust_row.iloc[0][
                        'Name'] if not current_cust_row.empty else (
                        cust_names[0] if cust_names else "Market")

                    st.subheader(f"Editing Order: {selected_reg_order}")

                    with st.form("edit_order_header_form"):
                        ec1, ec2, ec3 = st.columns(3)
                        new_cust_name = ec1.selectbox("Customer",
                                                      cust_names if cust_names else ["Market"],
                                                      index=cust_names.index(
                                                          current_cust_name) if cust_names and current_cust_name in cust_names else 0)

                        val_dp = safe_parse_date(order_header['DatePlaced'])
                        val_df = safe_parse_date(order_header['DateFullfilled'])
                        new_date_placed = ec2.date_input("Date Placed", value=val_dp)
                        new_date_fulfilled = ec3.date_input("Date Fulfilled", value=val_df)

                        update_header_btn = st.form_submit_button("💾 Update Order Details")
                        if update_header_btn:
                            try:
                                c_id = get_or_create_customer(new_cust_name)
                                get_or_create_order(selected_reg_order, c_id, str(new_date_placed),
                                                    str(new_date_fulfilled))
                                st.success("Order header details updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error updating order: {e}")

                    st.markdown("#### Sales Line Items in this Order")
                    if sales_items_df.empty:
                        st.warning("No sales items found for this order.")
                    else:
                        edited_sales_df = st.data_editor(
                            sales_items_df,
                            num_rows="dynamic",
                            use_container_width=True,
                            key="sales_editor",
                            column_config={
                                "SaleID": st.column_config.NumberColumn("Sale ID", disabled=True),
                                "ProductID": st.column_config.TextColumn("Product ID"),
                                "ProductName": st.column_config.TextColumn("Product Name"),
                                "BaseQuantitySold": st.column_config.NumberColumn("Base Qty Sold",
                                                                                  step=0.1),
                                "SalePrice": st.column_config.NumberColumn("Sale Price ($)",
                                                                           step=1.0)
                            }
                        )

                        if st.button("💾 Save Sales Line Items Changes", type="primary"):
                            try:
                                conn = sqlite3.connect(DB_FILENAME)
                                cursor = conn.cursor()

                                existing_sale_ids = set(sales_items_df['SaleID'].tolist())
                                updated_sale_ids = set(edited_sales_df['SaleID'].dropna().tolist())

                                deleted_ids = existing_sale_ids - updated_sale_ids
                                for d_id in deleted_ids:
                                    cursor.execute("DELETE FROM Sales WHERE SaleID = ?", (d_id,))

                                for _, row in edited_sales_df.iterrows():
                                    if pd.notna(row['SaleID']) and row[
                                        'SaleID'] in existing_sale_ids:
                                        cursor.execute('''
                                                        UPDATE Sales 
                                                        SET ProductID = ?, BaseQuantitySold = ?, SalePrice = ?
                                                        WHERE SaleID = ?
                                                    ''', (row['ProductID'], row['BaseQuantitySold'],
                                                          row['SalePrice'], row['SaleID']))
                                    else:
                                        cursor.execute('''
                                                        INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice)
                                                        VALUES (?, ?, ?, ?)
                                                    ''', (selected_reg_order, row['ProductID'],
                                                          row['BaseQuantitySold'],
                                                          row['SalePrice']))

                                conn.commit()
                                conn.close()
                                st.success("Sales items updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving sales items: {e}")

                    st.divider()
                    if st.button("🗑️ Delete Entire Order", type="secondary"):
                        try:
                            conn = sqlite3.connect(DB_FILENAME)
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM Sales WHERE OrderID = ?",
                                           (selected_reg_order,))
                            cursor.execute("DELETE FROM Orders WHERE OrderID = ?",
                                           (selected_reg_order,))
                            conn.commit()
                            conn.close()
                            st.success(f"Order {selected_reg_order} deleted successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting order: {e}")