import streamlit as st
import pandas as pd
import sqlite3
import os
import datetime
import io
import re
from difflib import get_close_matches
import altair as alt
import openpyxl

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
# Use the persistent storage directory on Fly.io, or fall back to local directory for development
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_FILENAME = os.path.join(DATA_DIR, "edwin_county_farms_sales.db")

# Hardcoded CSV Column Mapping Fallbacks
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
# Helper & Database Operations
# -----------------------------------------------------------------------------
def get_db_connection():
    """Returns a connection context manager for SQLite database operations."""
    return sqlite3.connect(DB_FILENAME)


def init_db():
    """Initializes the SQLite schema and performs automatic field migrations."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Customers (
                CustomerID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT UNIQUE
            )
        ''')

        cursor.execute("INSERT OR IGNORE INTO Customers (CustomerID, Name) VALUES (1, 'Market')")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Products (
                ProductID TEXT PRIMARY KEY,
                Name TEXT,
                BaseUnit TEXT,
                Category TEXT,
                SubCategory TEXT
            )
        ''')

        # Automatic schema migrations
        cursor.execute("PRAGMA table_info(Products)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if 'Category' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN Category TEXT")
        if 'SubCategory' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN SubCategory TEXT")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Orders (
                OrderID TEXT PRIMARY KEY,
                CustomerID INTEGER,
                DatePlaced TEXT,
                DateFullfilled TEXT,
                FOREIGN KEY(CustomerID) REFERENCES Customers(CustomerID)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Sales (
                SaleID INTEGER PRIMARY KEY AUTOINCREMENT,
                OrderID TEXT,
                ProductID TEXT,
                BaseQuantitySold REAL,
                SalePrice REAL,
                FOREIGN KEY(OrderID) REFERENCES Orders(OrderID),
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ProductMappings (
                ImportName TEXT PRIMARY KEY,
                ProductID TEXT,
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        ''')
        conn.commit()


def resolve_column(df_columns, primary_name, fallback_keywords):
    """
    Strictly resolves column names by trying exact primary matches, exact keyword matches,
    and partial matches while guarding against word overlaps (e.g. avoiding 'Product ID' when searching for 'Product').
    """
    # 1. Exact match for primary_name
    for col in df_columns:
        if str(col).strip().lower() == primary_name.strip().lower():
            return col

    # 2. Exact match for fallback keywords
    for kw in fallback_keywords:
        for col in df_columns:
            if str(col).strip().lower() == kw.strip().lower():
                return col

    # 3. Partial match with safeguards
    for kw in fallback_keywords:
        for col in df_columns:
            col_lower = str(col).strip().lower()
            kw_lower = kw.strip().lower()
            if kw_lower in col_lower:
                # Safeguard: Avoid matching 'Product ID' when looking for 'Product'
                if 'id' not in kw_lower and ' id' in col_lower:
                    continue
                return col

    return primary_name


def resolve_effective_subcategory(row):
    """
    Returns SubCategory if valid, or falls back to ProductName if
    Category or SubCategory is uncategorized/uncategorize/none/blank.
    """
    cat = str(row.get('Category', '')).strip().lower()
    subcat = str(row.get('SubCategory', '')).strip().lower()
    uncat_terms = {'uncategorized', 'uncategorize', 'none', 'nan', ''}

    if cat in uncat_terms or subcat in uncat_terms or pd.isna(row.get('SubCategory')):
        return str(row.get('ProductName', 'Unknown Product'))
    return str(row.get('SubCategory')).strip()


def safe_parse_date(date_val):
    """Safely converts arbitrary date strings to python datetime.date objects."""
    if pd.isna(date_val) or not date_val:
        return datetime.date.today()
    try:
        return pd.to_datetime(date_val).date()
    except Exception:
        return datetime.date.today()


def save_product_mapping(import_name, product_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO ProductMappings (ImportName, ProductID) 
            VALUES (?, ?)
            ON CONFLICT(ImportName) DO UPDATE SET ProductID = excluded.ProductID
        ''', (import_name, product_id))
        conn.commit()


def get_product_mapping(import_name):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM ProductMappings WHERE ImportName = ?', (import_name,))
        row = cursor.fetchone()
        return row[0] if row else None


def get_all_mappings_df():
    with get_db_connection() as conn:
        return pd.read_sql_query('''
            SELECT 
                pm.ImportName, 
                pm.ProductID, 
                p.Name AS DBProductName
            FROM ProductMappings pm
            LEFT JOIN Products p ON pm.ProductID = p.ProductID
        ''', conn)

def get_registered_order_ids():
    if not os.path.exists(DB_FILENAME):
        return set()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OrderID FROM Orders")
        return set(row[0] for row in cursor.fetchall())


def get_or_create_customer(name):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CustomerID FROM Customers WHERE Name = ?', (name,))
        row = cursor.fetchone()
        if row:
            customer_id = row[0]
        else:
            cursor.execute('INSERT INTO Customers (Name) VALUES (?)', (name,))
            customer_id = cursor.lastrowid
        conn.commit()
        return customer_id


def get_all_customers():
    with get_db_connection() as conn:
        return pd.read_sql_query('SELECT CustomerID, Name FROM Customers ORDER BY Name', conn)


def get_or_create_product(product_id, name, base_unit=None, category=None, sub_category=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM Products WHERE ProductID = ?', (product_id,))
        row = cursor.fetchone()
        if row:
            updates, params = [], []
            if name is not None:
                updates.append("Name = ?"); params.append(name)
            if base_unit is not None:
                updates.append("BaseUnit = ?"); params.append(base_unit)
            if category is not None:
                updates.append("Category = ?"); params.append(category)
            if sub_category is not None:
                updates.append("SubCategory = ?"); params.append(sub_category)

            if updates:
                params.append(product_id)
                cursor.execute(f"UPDATE Products SET {', '.join(updates)} WHERE ProductID = ?", params)
        else:
            unit = base_unit if base_unit is not None else "each"
            cat = category if category is not None else "Uncategorized"
            sub_cat = sub_category if sub_category is not None else "Uncategorized"
            cursor.execute('INSERT INTO Products (ProductID, Name, BaseUnit, Category, SubCategory) VALUES (?, ?, ?, ?, ?)',
                           (product_id, name, unit, cat, sub_cat))
        conn.commit()
        return product_id


def get_all_products():
    with get_db_connection() as conn:
        df_products = pd.read_sql_query(
            'SELECT ProductID, Name, BaseUnit, Category, SubCategory FROM Products ORDER BY Name', conn)

    if not df_products.empty:
        df_products['Display'] = df_products['ProductID'].astype(str) + " - " + df_products['Name']
        return df_products
    return pd.DataFrame(columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])


def get_or_create_order(order_id, customer_id, date_placed, date_fulfilled):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT OrderID FROM Orders WHERE OrderID = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute('''
                INSERT INTO Orders (OrderID, CustomerID, DatePlaced, DateFullfilled) 
                VALUES (?, ?, ?, ?)
            ''', (order_id, customer_id, date_placed, date_fulfilled))
        else:
            cursor.execute('''
                UPDATE Orders SET CustomerID = ?, DatePlaced = ?, DateFullfilled = ? WHERE OrderID = ?
            ''', (customer_id, date_placed, date_fulfilled, order_id))
        conn.commit()
        return order_id


def create_sale(order_id, product_id, base_quantity_sold, sale_price):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice) 
            VALUES (?, ?, ?, ?)
        ''', (order_id, product_id, base_quantity_sold, sale_price))
        conn.commit()


def load_sales_analytics_data():
    if not os.path.exists(DB_FILENAME):
        return pd.DataFrame()

    query = '''
        SELECT 
            s.SaleID,
            o.OrderID,
            o.DateFullfilled AS FulfillmentDate,
            o.DatePlaced AS DatePlaced,
            c.CustomerID,
            c.Name AS CustomerName,
            p.ProductID,
            p.Name AS ProductName,
            p.BaseUnit,
            COALESCE(p.Category, 'Uncategorized') AS Category,
            COALESCE(p.SubCategory, 'Uncategorized') AS SubCategory,
            s.BaseQuantitySold,
            s.SalePrice
        FROM Sales s
        JOIN Orders o ON s.OrderID = o.OrderID
        JOIN Customers c ON o.CustomerID = c.CustomerID
        JOIN Products p ON s.ProductID = p.ProductID
    '''
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query(query, conn)
    except Exception:
        df = pd.DataFrame()

    if not df.empty:
        df['FulfillmentDatetime'] = pd.to_datetime(df['FulfillmentDate'], errors='coerce')
        df['FulfillmentDateObj'] = df['FulfillmentDatetime'].dt.date
        df['Week'] = df['FulfillmentDatetime'].dt.to_period('W').astype(str)
        df['Month'] = df['FulfillmentDatetime'].dt.to_period('M').astype(str)
        df['Year'] = df['FulfillmentDatetime'].dt.year.fillna(0).astype(int)

    return df


def load_and_prepare_csv(filepath):
    if not os.path.exists(filepath):
        return None
    return pd.read_csv(filepath)


def parse_csv_to_scratch(external_df, db_products_df=None):
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
            import_name = f"{name}\n{variant}"
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
                try:
                    qty = float(str(row[q_col]).replace(',', '.'))
                    break
                except Exception:
                    pass

        price = 0.0
        for p_col in ['Ventes nettes', 'Ventes brutes', 'Subtotal', 'Total', 'Price']:
            if p_col in row and pd.notna(row[p_col]):
                val_str = str(row[p_col]).replace('$', '').replace('€', '').replace(' ', '').replace(',', '.')
                try:
                    price = float(val_str)
                    break
                except Exception:
                    pass

        items.append({
            "Select": False,
            "ImportName": import_name,
            "ProductID": matched_prod_id,
            "ProductName": matched_prod_name,
            "BaseQuantitySold": qty,
            "SalePrice": price
        })
    return pd.DataFrame(items)


# -----------------------------------------------------------------------------
# Streamlit UI Setup
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Edwin County Farms - Sales Management", layout="wide")
init_db()

st.title("Edwin County Farms - Sales Management System")

tab1, tab2, tab3, tab4 = st.tabs([
    "📝 Sales Entry & Validation",
    "📊 Sales Analysis Dashboard",
    "🥬 Product Management",
    "📦 Order Management"
])

# =============================================================================
# TAB 1: SALES ENTRY & VALIDATION
# =============================================================================
# =============================================================================
# TAB 1: SALES ENTRY & VALIDATION
# =============================================================================
with tab1:
    st.header("📝 Sales Entry & Validation")
    uploaded_tab1_csv = st.file_uploader(
        "Upload Sales Export CSV for Validation",
        type=["csv"],
        key="tab1_csv_uploader"
    )

    if uploaded_tab1_csv is not None:
        try:
            df_raw = pd.read_csv(uploaded_tab1_csv)
        except Exception:
            uploaded_tab1_csv.seek(0)
            try:
                df_raw = pd.read_csv(uploaded_tab1_csv, encoding='utf-16', sep='\t')
            except Exception:
                uploaded_tab1_csv.seek(0)
                df_raw = pd.read_csv(uploaded_tab1_csv, encoding='windows-1252', sep=';')

        # --- Continue with your existing column resolution and pending logic below ---
        cols = list(df_raw.columns)
        # ... rest of your Tab 1 code ...
    else:
        st.info("Please upload a CSV file above to review and validate pending sales orders.")

    if df_raw is not None:
        cols = list(df_raw.columns)

        # Automatic strict column resolution via hardcoded mappings
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

        registered_db_orders = get_registered_order_ids()
        valid_df = df_raw[~df_raw[order_status_col].astype(str).str.upper().str.contains('CANCEL', na=False)].copy()

        all_raw_dates = valid_df[date_fulfilled_col].dropna().unique()
        truly_pending_dates = []

        for d in all_raw_dates:
            df_d = valid_df[valid_df[date_fulfilled_col] == d]
            is_guest_d = df_d[customer_col].str.startswith("Guest customer", na=False)
            df_reg_d = df_d[~is_guest_d]
            df_gst_d = df_d[is_guest_d]

            reg_pending = not df_reg_d[order_id_col].astype(str).isin(
                registered_db_orders).all() if not df_reg_d.empty else False
            guest_pending = (not df_gst_d.empty) and (f"GUEST-{d}" not in registered_db_orders)

            if reg_pending or guest_pending:
                truly_pending_dates.append(d)

        if len(truly_pending_dates) == 0:
            st.success("✨ All valid records have been registered!")
        else:
            db_products_df_bulk = get_all_products()
            valid_db_pids = set(
                db_products_df_bulk['ProductID'].astype(str).tolist()) if not db_products_df_bulk.empty else set()

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
                    grouped = df_guests.groupby(
                        [product_id_col, product_col, item_unit_col, category_col, sub_category_col])
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
                            try:
                                ni = float(row[num_items_col]) if pd.notna(row[num_items_col]) else 1.0
                            except Exception:
                                ni = 1.0
                            try:
                                q = float(row[qty_col]) if pd.notna(row[qty_col]) else 0.0
                            except Exception:
                                q = 0.0
                            total_base_qty += (ni * q)

                            try:
                                sub = float(str(row[subtotal_col]).replace('$', '').replace(',', '')) if pd.notna(
                                    row[subtotal_col]) else 0.0
                            except Exception:
                                sub = 0.0
                            total_subtotal += sub

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

                        try:
                            ni = float(row[num_items_col]) if pd.notna(row[num_items_col]) else 1.0
                        except Exception:
                            ni = 1.0
                        try:
                            q = float(row[qty_col]) if pd.notna(row[qty_col]) else 0.0
                        except Exception:
                            q = 0.0
                        calc_base_qty = ni * q

                        try:
                            subtotal_val = float(str(row[subtotal_col]).replace('$', '').replace(',', ''))
                        except Exception:
                            subtotal_val = 0.0

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
                st.info(
                    f"✨ Found **{len(auto_ready_orders)}** order(s) where all products are natively mapped to the database.")
                if st.button(f"🚀 Bulk Validate & Save Ready Orders ({len(auto_ready_orders)})", type="primary"):
                    with st.spinner("Saving mapped orders..."):
                        for o_data in auto_ready_orders:
                            c_id = get_or_create_customer(o_data['customer_name'])
                            get_or_create_order(o_data['order_id'], c_id, o_data['date_placed'],
                                                o_data['date_fulfilled'])
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
                grouped = df_guests.groupby(
                    [product_id_col, product_col, item_unit_col, category_col, sub_category_col])
                for (p_id, p_name, p_unit, p_cat, p_subcat), group in grouped:
                    total_base_qty = 0.0
                    total_subtotal = 0.0

                    for idx, row in group.iterrows():
                        try:
                            ni = float(row[num_items_col]) if pd.notna(row[num_items_col]) else 1.0
                        except Exception:
                            ni = 1.0
                        try:
                            q = float(row[qty_col]) if pd.notna(row[qty_col]) else 0.0
                        except Exception:
                            q = 0.0
                        total_base_qty += (ni * q)

                        try:
                            sub = float(str(row[subtotal_col]).replace('$', '').replace(',', '')) if pd.notna(
                                row[subtotal_col]) else 0.0
                        except Exception:
                            sub = 0.0
                        total_subtotal += sub

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
                st.info(
                    f"All orders and market sales for {selected_date} have already been registered in the database.")
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
                        raw_total = o_df.iloc[0][total_col]
                        try:
                            total_val = float(str(raw_total).replace('$', '').replace(',', ''))
                            total_str = f"${total_val:.2f}"
                        except Exception:
                            total_str = str(raw_total)
                        return f"Order {o_id} | {c_name} | Total: {total_str} | Placed: {d_placed}"

                selected_order = st.selectbox("2. Select Order Number", dropdown_order_options,
                                              format_func=format_order_display)

                if selected_order:
                    try:
                        db_products_df = get_all_products()
                        product_display_list = db_products_df['Display'].tolist() if not db_products_df.empty else []
                    except Exception:
                        product_display_list = []
                        db_products_df = pd.DataFrame(
                            columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])

                    st.divider()
                    is_guest_selection = (selected_order == guest_order_key)

                    if is_guest_selection:
                        st.markdown(f"### 📦 Consolidated Guest Order: {selected_date}")
                        with st.container():
                            c1, c2, c3, c4 = st.columns(4)
                            customer_input = c1.text_input("Customer Name", value="Market")
                            order_input = c2.text_input("Order ID", value=guest_order_id)
                            date_p_input = c3.date_input("Date Placed",
                                                         value=safe_parse_date(df_guests.iloc[0][date_placed_col]))
                            date_f_input = c4.date_input("Date Fulfilled", value=safe_parse_date(selected_date))
                    else:
                        df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                        st.markdown(f"### 📦 Order Details: {selected_order}")
                        first_row = df_order.iloc[0]
                        with st.container():
                            c1, c2, c3, c4 = st.columns(4)
                            customer_input = c1.text_input("Customer Name", value=str(first_row[customer_col]))
                            order_input = c2.text_input("Order ID", value=str(selected_order))
                            date_p_input = c3.date_input("Date Placed",
                                                         value=safe_parse_date(first_row[date_placed_col]))
                            date_f_input = c4.date_input("Date Fulfilled",
                                                         value=safe_parse_date(first_row[date_fulfilled_col]))

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
                                    matched_db_row = db_products_df[
                                        db_products_df['ProductID'].astype(str) == mapped_pid]
                                else:
                                    matched_db_row = db_products_df[
                                        db_products_df['ProductID'].astype(str) == csv_prod_id]

                                default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                selected_db_product = sc1.selectbox(f"Database Mapping [Guest {idx}]", dropdown_options,
                                                                    index=default_idx, key=f"g_dd_{idx}")

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
                                    'original_name': csv_prod_name, 'prod_id': final_prod_id,
                                    'prod_name': final_prod_name,
                                    'base_unit': base_unit, 'category': None, 'sub_category': None,
                                    'qty_sold': qty_sold_input, 'sale_price': sale_price_input
                                })
                                st.divider()
                    else:
                        df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                        for idx, row in df_order.iterrows():
                            with st.container():
                                csv_prod_id = str(row[product_id_col])
                                csv_prod_name = str(row[product_col])
                                csv_item_unit = str(row[item_unit_col]) if pd.notna(row[item_unit_col]) else "each"

                                try:
                                    num_items = float(row[num_items_col]) if pd.notna(row[num_items_col]) else 1.0
                                except ValueError:
                                    num_items = 1.0
                                try:
                                    qty = float(row[qty_col]) if pd.notna(row[qty_col]) else 0.0
                                except ValueError:
                                    qty = 0.0
                                calc_base_qty = num_items * qty

                                try:
                                    subtotal_val = float(str(row[subtotal_col]).replace('$', '').replace(',', ''))
                                except ValueError:
                                    subtotal_val = 0.0

                                st.markdown(f"📌 **CSV Item:** `{csv_prod_name}`")

                                sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1.8, 1.2, 1.2, 0.8, 0.8, 0.8])

                                dropdown_options = ["++ Create New Product ++"] + product_display_list
                                mapped_pid = get_product_mapping(csv_prod_name)
                                if mapped_pid:
                                    matched_db_row = db_products_df[
                                        db_products_df['ProductID'].astype(str) == mapped_pid]
                                else:
                                    matched_db_row = db_products_df[
                                        db_products_df['ProductID'].astype(str) == csv_prod_id]

                                default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                selected_db_product = sc1.selectbox(f"Database Mapping [{idx}]", dropdown_options,
                                                                    index=default_idx, key=f"dd_{idx}")

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
                                    'original_name': csv_prod_name, 'prod_id': final_prod_id,
                                    'prod_name': final_prod_name,
                                    'base_unit': base_unit, 'category': None, 'sub_category': None,
                                    'qty_sold': qty_sold_input, 'sale_price': sale_price_input
                                })
                                st.divider()

                    if st.button("✅ Validate & Save Order", type="primary", use_container_width=True):
                        try:
                            c_id = get_or_create_customer(customer_input)
                            o_id = get_or_create_order(order_input, c_id, str(date_p_input), str(date_f_input))
                            for sale in sale_inputs:
                                p_id = get_or_create_product(sale['prod_id'], sale['prod_name'], sale['base_unit'],
                                                             sale['category'], sale['sub_category'])
                                create_sale(o_id, p_id, sale['qty_sold'], sale['sale_price'])
                                if sale['original_name']:
                                    save_product_mapping(sale['original_name'], p_id)
                            st.success(f"Successfully processed Order {order_input} with {len(sale_inputs)} items!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error saving to database: {e}")
    else:
        st.error(f"Could not find `{CSV_FILENAME}`. Please verify the file is in the same directory as this script.")

# =============================================================================
# TAB 2: SALES ANALYSIS DASHBOARD
# =============================================================================
with tab2:
    st.header("📈 Sales Analytics & Planning Dashboard")

    analytics_df = load_sales_analytics_data()

    if analytics_df.empty:
        st.info("No sales records found in the database yet. Validate and save orders in the first tab to view analytics.")
    else:
        subtab1, subtab2 = st.tabs(["📊 General Dashboard", "📅 YoY Comparison & Planning"])

        # ---------------------------------------------------------------------
        # SUB-TAB 1: GENERAL DASHBOARD
        # ---------------------------------------------------------------------
        with subtab1:
            st.subheader("Filter Analytics by Period")

            f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns(5)

            min_date = analytics_df['FulfillmentDateObj'].min() or datetime.date.today()
            max_date = analytics_df['FulfillmentDateObj'].max() or datetime.date.today()

            date_selection = f_col1.date_input("Select Date Range", value=(min_date, max_date), min_value=min_date,
                                               max_value=max_date, key="gen_date_range")
            if isinstance(date_selection, tuple):
                start_date = date_selection[0] if len(date_selection) >= 1 else min_date
                end_date = date_selection[1] if len(date_selection) == 2 else start_date
            else:
                start_date = end_date = date_selection

            selected_categories = f_col2.multiselect("Filter by Category", sorted(analytics_df['Category'].unique()), key="gen_cat_multi")
            selected_subcategories = f_col3.multiselect("Filter by Sub-Category", sorted(analytics_df['SubCategory'].dropna().unique()), key="gen_subcat_multi")
            selected_customers = f_col4.multiselect("Filter by Customer", sorted(analytics_df['CustomerName'].unique()), key="gen_cust_multi")
            selected_products = f_col5.multiselect("Filter by Product", sorted(analytics_df['ProductName'].unique()), key="gen_prod_multi")

            filtered_df = analytics_df[
                (analytics_df['FulfillmentDateObj'] >= start_date) & (analytics_df['FulfillmentDateObj'] <= end_date)
            ]

            if selected_customers:
                filtered_df = filtered_df[filtered_df['CustomerName'].isin(selected_customers)]
            if selected_categories:
                filtered_df = filtered_df[filtered_df['Category'].isin(selected_categories)]
            if selected_subcategories:
                filtered_df = filtered_df[filtered_df['SubCategory'].isin(selected_subcategories)]
            if selected_products:
                filtered_df = filtered_df[filtered_df['ProductName'].isin(selected_products)]

            st.caption(f"Showing **{len(filtered_df)}** sales line items matching current filters.")
            st.divider()

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Revenue", f"${filtered_df['SalePrice'].sum():,.2f}")
            col2.metric("Total Units Sold", f"{filtered_df['BaseQuantitySold'].sum():,.1f}")
            col3.metric("Total Orders", f"{filtered_df['OrderID'].nunique():,}")
            col4.metric("Unique Customers", f"{filtered_df['CustomerName'].nunique():,}")

            st.divider()

            chart_col1, chart_col2 = st.columns(2)
            with chart_col1:
                st.subheader("Revenue by Customer ($)")
                if not filtered_df.empty:
                    cust_rev = filtered_df.groupby('CustomerName')['SalePrice'].sum().reset_index().sort_values(by='SalePrice', ascending=False).set_index('CustomerName')
                    st.bar_chart(cust_rev)

            with chart_col2:
                st.subheader("Revenue by Category ($)")
                if not filtered_df.empty:
                    cat_rev = filtered_df.groupby('Category')['SalePrice'].sum().reset_index().sort_values(by='SalePrice', ascending=False).set_index('Category')
                    st.bar_chart(cat_rev)

            st.divider()

            st.subheader("📦 Product-Level Sales Summary")
            if not filtered_df.empty:
                group_by_subcat = st.checkbox("Group summary by Sub-Category", value=False, key="group_by_subcat_toggle")

                working_df = filtered_df.copy()
                working_df['EffectiveSubCategory'] = working_df.apply(resolve_effective_subcategory, axis=1)

                if group_by_subcat:
                    product_summary = working_df.groupby(['Category', 'EffectiveSubCategory']).agg(
                        ProductName=('ProductName', lambda x: f"{x.nunique()} product(s)" if x.nunique() > 1 else x.iloc[0]),
                        Total_Base_Quantity_Sold=('BaseQuantitySold', 'sum'),
                        Total_Revenue=('SalePrice', 'sum'),
                        Times_Ordered=('OrderID', 'nunique')
                    ).reset_index().rename(columns={'EffectiveSubCategory': 'SubCategory'}).sort_values(by='Total_Revenue', ascending=False)
                    filename_suffix = "sub_category_summary"
                else:
                    product_summary = working_df.groupby(['ProductID', 'ProductName', 'Category', 'SubCategory', 'BaseUnit']).agg(
                        Total_Base_Quantity_Sold=('BaseQuantitySold', 'sum'),
                        Total_Revenue=('SalePrice', 'sum'),
                        Times_Ordered=('OrderID', 'nunique')
                    ).reset_index().sort_values(by='Total_Revenue', ascending=False)
                    filename_suffix = "product_summary"

                st.dataframe(product_summary, use_container_width=True)

                st.download_button(
                    label="📥 Download Summary as CSV",
                    data=product_summary.to_csv(index=False).encode('utf-8'),
                    file_name=f"edwin_county_farms_{filename_suffix}_{start_date}_to_{end_date}.csv",
                    mime="text/csv"
                )

                st.divider()
                st.subheader("📅 Export Weekly Sales to Excel")
                export_metric = st.radio("Select metric to aggregate in export:", ["Revenue ($)", "Quantity (Units)"], horizontal=True, key="excel_export_metric")

                metric_col = 'SalePrice' if 'Revenue' in export_metric else 'BaseQuantitySold'
                pivot_df = pd.pivot_table(filtered_df, values=metric_col,
                                          index=['Category', 'SubCategory', 'ProductID', 'ProductName'], columns='Week',
                                          aggfunc='sum', fill_value=0).reset_index()

                excel_buffer = io.BytesIO()
                with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                    pivot_df.to_excel(writer, index=False, sheet_name='Weekly Sales')

                st.download_button(
                    label="📥 Download Weekly Sales Export (.xlsx)",
                    data=excel_buffer.getvalue(),
                    file_name=f"Weekly_Sales_Export_{datetime.datetime.now().strftime('%Y%m%d')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        # ---------------------------------------------------------------------
        # SUB-TAB 2: YoY COMPARISON & PLANNING
        # ---------------------------------------------------------------------
        with subtab2:
            st.subheader("📅 YoY Crop & Sales Planning")
            st.write("Select a specific 'Season Window' to directly compare sales metrics (e.g., April 1st to May 12th) across different years.")

            st.markdown("##### 1. Define Season Window")
            p_col1, p_col2 = st.columns(2)
            season_start = p_col1.date_input("Start of Window (Year is ignored)", value=datetime.date(2024, 4, 1),
                                             min_value=datetime.date(2024, 1, 1), max_value=datetime.date(2024, 12, 31),
                                             key="yoy_start_date")
            season_end = p_col2.date_input("End of Window (Year is ignored)", value=datetime.date(2024, 5, 12),
                                           min_value=datetime.date(2024, 1, 1), max_value=datetime.date(2024, 12, 31),
                                           key="yoy_end_date")

            def map_to_dummy_year(date_obj):
                if pd.isna(date_obj): return pd.NaT
                if date_obj.month == 2 and date_obj.day == 29: return datetime.date(2024, 2, 29)
                return datetime.date(2024, date_obj.month, date_obj.day)

            yoy_base_df = analytics_df.copy()
            yoy_base_df['DummyDate'] = yoy_base_df['FulfillmentDateObj'].apply(map_to_dummy_year)

            yoy_filtered = yoy_base_df[
                (yoy_base_df['DummyDate'] >= season_start) & (yoy_base_df['DummyDate'] <= season_end)
            ]

            st.markdown("##### 2. Apply Custom Filters")
            pf_col1, pf_col2, pf_col3, pf_col4, pf_col5 = st.columns(5)

            available_years = sorted(yoy_filtered['Year'].unique().tolist(), reverse=True)
            filter_yoy_years = pf_col1.multiselect("Select Years to Compare", available_years, default=available_years, key="yoy_year")
            filter_yoy_cats = pf_col2.multiselect("Filter by Category", sorted(yoy_filtered['Category'].unique()), default=[], key="yoy_cat")
            filter_yoy_subcats = pf_col3.multiselect("Filter by Sub-Category", sorted(yoy_filtered['SubCategory'].unique()), default=[], key="yoy_subcat")
            filter_yoy_custs = pf_col4.multiselect("Filter by Customer", sorted(yoy_filtered['CustomerName'].unique()), default=[], key="yoy_cust")
            filter_yoy_prods = pf_col5.multiselect("Filter by Product", sorted(yoy_filtered['ProductName'].unique()), default=[], key="yoy_prod")

            if filter_yoy_years: yoy_filtered = yoy_filtered[yoy_filtered['Year'].isin(filter_yoy_years)]
            if filter_yoy_cats: yoy_filtered = yoy_filtered[yoy_filtered['Category'].isin(filter_yoy_cats)]
            if filter_yoy_subcats: yoy_filtered = yoy_filtered[yoy_filtered['SubCategory'].isin(filter_yoy_subcats)]
            if filter_yoy_custs: yoy_filtered = yoy_filtered[yoy_filtered['CustomerName'].isin(filter_yoy_custs)]
            if filter_yoy_prods: yoy_filtered = yoy_filtered[yoy_filtered['ProductName'].isin(filter_yoy_prods)]

            st.divider()

            if yoy_filtered.empty:
                st.warning("No sales found for the selected season window and filters.")
            else:
                yoy_metric = st.radio("Compare Metrics Using:", ["Quantity (Units)", "Revenue ($)"], horizontal=True, key="yoy_metric_radio")
                target_col = 'BaseQuantitySold' if 'Quantity' in yoy_metric else 'SalePrice'

                st.markdown(f"#### 📈 {yoy_metric} Timeline Trend")
                time_view = st.radio("Aggregate Timeline By:", ["Daily", "Weekly", "Monthly"], horizontal=True,
                                     key="yoy_time_agg")

                trend_base_df = yoy_filtered.copy()
                if time_view == "Daily":
                    trend_base_df['PeriodStart'] = trend_base_df['DummyDate']
                elif time_view == "Weekly":
                    trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']) - pd.to_timedelta(
                        pd.to_datetime(trend_base_df['DummyDate']).dt.weekday, unit='d')
                    trend_base_df['PeriodStart'] = trend_base_df['PeriodStart'].dt.date
                elif time_view == "Monthly":
                    # Snap to the 1st of the respective month
                    trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']).apply(
                        lambda x: datetime.date(x.year, x.month, 1))

                agg_df = trend_base_df.groupby(['Year', 'PeriodStart'])[target_col].sum().reset_index()
                agg_df['YearLabel'] = agg_df['Year'].astype(str)

                # Format tooltips and axis differently for Monthly views
                if time_view == "Monthly":
                    agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%B')
                    axis_format = '%B'
                else:
                    agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%d-%b')
                    axis_format = '%b %d'

                chart = alt.Chart(agg_df).mark_line(point=True).encode(
                    x=alt.X('PeriodStart:T', title='Time of Year', axis=alt.Axis(format=axis_format)),
                    y=alt.Y(f'{target_col}:Q', title=yoy_metric),
                    color=alt.Color('YearLabel:N', title='Year'),
                    tooltip=['FullDateLabel:N', alt.Tooltip(f'{target_col}:Q', format=',.2f'), 'YearLabel:N']
                ).interactive()

                st.altair_chart(chart, use_container_width=True)

                st.divider()
                st.markdown(f"#### 📊 Product Comparison: {season_start.strftime('%B %d')} to {season_end.strftime('%B %d')}")

                chart_df = yoy_filtered.groupby(['ProductName', 'Year'])[target_col].sum().reset_index()
                chart_df['YearStr'] = chart_df['Year'].astype(str)

                yoy_chart = alt.Chart(chart_df).mark_bar().encode(
                    x=alt.X('ProductName:N', title='Product', axis=alt.Axis(labelAngle=-45, labelLimit=250)),
                    xOffset='YearStr:N',
                    y=alt.Y(f'{target_col}:Q', title=yoy_metric),
                    color=alt.Color('YearStr:N', title='Year'),
                    tooltip=['ProductName:N', 'YearStr:N', alt.Tooltip(f'{target_col}:Q', format=',.2f')]
                ).configure_view(stroke='transparent')

                st.altair_chart(yoy_chart, use_container_width=True)

                # --- YoY Data Matrix with Sub-Category Grouping Option ---
                st.write("###### Data Matrix")
                group_yoy_matrix_subcat = st.checkbox("Group matrix by Sub-Category", value=False, key="yoy_matrix_subcat_toggle")

                yoy_matrix_df = yoy_filtered.copy()
                yoy_matrix_df['EffectiveSubCategory'] = yoy_matrix_df.apply(resolve_effective_subcategory, axis=1)

                if group_yoy_matrix_subcat:
                    pivot_index = ['Category', 'EffectiveSubCategory']
                    index_names = ['Category', 'SubCategory']
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

                # Append Total Row
                if not yoy_pivot.empty:
                    col_sums = yoy_pivot.sum(numeric_only=True)
                    if group_yoy_matrix_subcat:
                        total_index = pd.MultiIndex.from_tuples([("TOTAL", "")], names=index_names)
                    else:
                        total_index = pd.MultiIndex.from_tuples([("TOTAL", "", "")], names=index_names)

                    total_df = pd.DataFrame([col_sums], index=total_index)
                    yoy_pivot_with_total = pd.concat([yoy_pivot, total_df])
                else:
                    yoy_pivot_with_total = yoy_pivot

                st.dataframe(yoy_pivot_with_total, use_container_width=True)


# =============================================================================
# TAB 3: PRODUCT MANAGEMENT (WITH NESTED SUB-TABS)
# =============================================================================
@st.dialog("Merge Products")
def merge_dialog(selected_df):
    st.warning("Merging will update all historical sales and mappings to point to the kept product. The other products will be permanently deleted.")

    st.write("Selected Products to Merge:")
    st.dataframe(selected_df[['ProductID', 'Name', 'Category', 'SubCategory']], hide_index=True)

    keep_options = selected_df['ProductID'].astype(str) + " - " + selected_df['Name']
    target_selection = st.selectbox("Which product do you want to KEEP as the primary?", keep_options.tolist())

    if st.button("🚨 Confirm Merge", type="primary", use_container_width=True):
        target_pid = target_selection.split(" - ", 1)[0]
        other_pids = selected_df[selected_df['ProductID'] != target_pid]['ProductID'].tolist()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(other_pids))

                cursor.execute(f"UPDATE Sales SET ProductID = ? WHERE ProductID IN ({placeholders})", [target_pid] + other_pids)
                cursor.execute(f"UPDATE ProductMappings SET ProductID = ? WHERE ProductID IN ({placeholders})", [target_pid] + other_pids)
                cursor.execute(f"DELETE FROM Products WHERE ProductID IN ({placeholders})", other_pids)
                conn.commit()

            st.success("Products merged successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"Error merging products: {e}")


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
            st.write("Manage the single shared mapping table used across all CSV imports (Tab 1 & Tab 4).")

            mappings_df = get_all_mappings_df()

            if mappings_df.empty:
                st.info(
                    "No mappings saved yet. Map products in the Sales Entry or Market Importer tab to save them automatically.")
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
                        "ProductID": st.column_config.SelectboxColumn("Mapped DB ProductID", options=db_prod_options,
                                                                      required=True),
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
# TAB 4: ORDER MANAGEMENT & MARKET IMPORTER
# =============================================================================

@st.dialog("Define New Products", width="large")
def define_new_products_dialog(import_names):
    st.write("Please set the properties for the newly mapped products below:")
    with st.form("new_products_form"):
        new_prods = []
        for i, csv_name in enumerate(import_names):
            st.markdown(f"**Original CSV Name:** `{csv_name}`")
            c1, c2, c3, c4, c5 = st.columns(5)

            # Auto-generate a basic slugified ID
            slug = re.sub(r'[^a-z0-9]+', '-', str(csv_name).lower()).strip('-')
            if not slug: slug = "item"
            default_id = f"{slug}-{datetime.datetime.now().strftime('%M%S')}-{i}"

            pid = c1.text_input("Product ID", value=default_id, key=f"np_id_{i}")
            pname = c2.text_input("Product Name", value=str(csv_name), key=f"np_name_{i}")
            punit = c3.text_input("Unit", value="each", key=f"np_unit_{i}")
            pcat = c4.text_input("Category", value="Produce", key=f"np_cat_{i}")
            psub = c5.text_input("Sub-Category", value="Uncategorized", key=f"np_sub_{i}")
            new_prods.append((csv_name, pid, pname, punit, pcat, psub))

        if st.form_submit_button("💾 Save New Products"):
            for csv_name, pid, pname, punit, pcat, psub in new_prods:
                get_or_create_product(pid.strip(), pname.strip(), punit.strip(), pcat.strip(), psub.strip())
                save_product_mapping(csv_name, pid.strip())

                # Update the session state to reflect the new mapping automatically
                display_str = f"{pid.strip()} - {pname.strip()}"
                mask = st.session_state["market_draft_df"]["ImportName"] == csv_name
                st.session_state["market_draft_df"].loc[mask, "Selected DB Product"] = display_str

            st.success("New products created! Please re-click 'Aggregate & Save Market Order' to finalize the import.")
            st.rerun()

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

        uploaded_file = st.file_uploader("Upload market POS CSV", type=["csv"], key="market_csv_upload")

        extracted_date = datetime.date.today()
        if uploaded_file:
            date_match = re.search(r'(\d{4}-\d{2}-\d{2})', uploaded_file.name)
            if date_match:
                try:
                    extracted_date = datetime.datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    pass

        with st.container():
            c1, c2, c3, c4 = st.columns(4)
            default_order_id = f"MARKET-{extracted_date.strftime('%Y%m%d')}"
            new_order_id = c1.text_input("Order ID", value=default_order_id, key="market_order_id")
            new_customer = c2.text_input("Customer Name", value="Market", key="market_customer_name")
            new_date_placed = c3.date_input("Date Placed", value=extracted_date, key="market_date_placed")
            new_date_fulfilled = c4.date_input("Date Fulfilled", value=extracted_date, key="market_date_fulfilled")

        if uploaded_file and product_display_list:
            if "market_csv_name" not in st.session_state or st.session_state["market_csv_name"] != uploaded_file.name:
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

                # Default to "++ Create New Product ++" if the product has never been mapped before
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
                elif edited_market_df["Selected DB Product"].isna().any() or edited_market_df["Selected DB Product"].eq(
                        "").any():
                    st.error("Please ensure all items are mapped to a Database Product.")
                else:
                    # Check if the user has requested any new product creations
                    needs_creation = edited_market_df[
                        edited_market_df["Selected DB Product"] == "++ Create New Product ++"]

                    if not needs_creation.empty:
                        # Extract the unique names that need to be generated and trigger the dialog
                        import_names_to_create = needs_creation["ImportName"].unique().tolist()
                        define_new_products_dialog(import_names_to_create)
                    else:
                        # If everything is mapped to an existing product, proceed with saving the order natively
                        with st.spinner("Saving mappings and aggregating order items..."):
                            edited_market_df['FinalProductID'] = edited_market_df['Selected DB Product'].map(
                                display_to_id)

                            for _, row in edited_market_df.iterrows():
                                csv_name = row['ImportName']
                                p_id = row['FinalProductID']
                                if pd.notna(csv_name) and str(csv_name).strip() != "" and pd.notna(p_id):
                                    save_product_mapping(str(csv_name).strip(), str(p_id))

                            aggregated_df = edited_market_df.groupby('FinalProductID').agg({
                                'BaseQuantitySold': 'sum',
                                'SalePrice': 'sum'
                            }).reset_index()

                            try:
                                c_id = get_or_create_customer(new_customer)
                                get_or_create_order(new_order_id, c_id, str(new_date_placed), str(new_date_fulfilled))

                                for _, row in aggregated_df.iterrows():
                                    create_sale(new_order_id, row['FinalProductID'], float(row['BaseQuantitySold']),
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
        st.subheader("🔍 Search & Edit Existing Registered Orders")

        orders_query = '''
            SELECT 
                o.OrderID, 
                c.Name AS CustomerName, 
                o.DatePlaced, 
                o.DateFullfilled,
                COALESCE(SUM(s.SalePrice), 0.0) AS TotalOrderPrice,
                GROUP_CONCAT(p.Name, '||') AS ContainedProductNames
            FROM Orders o
            JOIN Customers c ON o.CustomerID = c.CustomerID
            LEFT JOIN Sales s ON o.OrderID = s.OrderID
            LEFT JOIN Products p ON s.ProductID = p.ProductID
            GROUP BY o.OrderID, c.Name, o.DatePlaced, o.DateFullfilled
            ORDER BY o.DateFullfilled DESC
        '''
        with get_db_connection() as conn:
            registered_orders_df = pd.read_sql_query(orders_query, conn)

        if registered_orders_df.empty:
            st.info("No registered orders found in the database yet.")
        else:
            registered_orders_df['DateFullfilled_Obj'] = pd.to_datetime(
                registered_orders_df['DateFullfilled'], errors='coerce'
            ).dt.date

            st.markdown("##### 🔎 Filter Orders")
            f_col1, f_col2, f_col3, f_col4 = st.columns(4)

            filter_customers = f_col1.multiselect("Filter by Customer", sorted(registered_orders_df['CustomerName'].dropna().unique()), key="edit_search_cust")
            filter_dates = f_col2.multiselect("Filter by Date Fulfilled", sorted(registered_orders_df['DateFullfilled_Obj'].dropna().unique(), reverse=True, key=str), key="edit_search_date")

            db_prods = get_all_products()
            all_prod_names = sorted(db_prods['Name'].tolist()) if not db_prods.empty else []
            filter_products = f_col3.multiselect("Filter by Product Contained", all_prod_names, key="edit_search_prod")

            max_possible_price = float(registered_orders_df['TotalOrderPrice'].max()) if not registered_orders_df.empty else 1000.0
            price_range = f_col4.slider("Filter by Order Total ($)", min_value=0.0, max_value=max(max_possible_price, 100.0), value=(0.0, max(max_possible_price, 100.0)), step=5.0, key="edit_search_price_slider")

            filtered_orders_df = registered_orders_df.copy()

            if filter_customers:
                filtered_orders_df = filtered_orders_df[filtered_orders_df['CustomerName'].isin(filter_customers)]
            if filter_dates:
                filtered_orders_df = filtered_orders_df[filtered_orders_df['DateFullfilled_Obj'].isin(filter_dates)]
            if filter_products:
                def contains_any_prod(prod_concat_str):
                    if not prod_concat_str or pd.isna(prod_concat_str):
                        return False
                    prods_in_order = set(prod_concat_str.split('||'))
                    return any(p in prods_in_order for p in filter_products)

                filtered_orders_df = filtered_orders_df[filtered_orders_df['ContainedProductNames'].apply(contains_any_prod)]

            filtered_orders_df = filtered_orders_df[
                (filtered_orders_df['TotalOrderPrice'] >= price_range[0]) &
                (filtered_orders_df['TotalOrderPrice'] <= price_range[1])
            ]

            st.caption(f"Found **{len(filtered_orders_df)}** matching order(s).")
            st.divider()

            if filtered_orders_df.empty:
                st.warning("No orders match the selected search filters.")
            else:
                def format_reg_order_display(o_id):
                    m_row = filtered_orders_df[filtered_orders_df['OrderID'] == o_id].iloc[0]
                    return f"Order: {o_id} | Customer: {m_row['CustomerName']} | Total: ${m_row['TotalOrderPrice']:.2f} | Fulfilled: {m_row['DateFullfilled']}"

                selected_edit_order_id = st.selectbox(
                    "Select Order to Edit",
                    filtered_orders_df['OrderID'].tolist(),
                    format_func=format_reg_order_display,
                    key="edit_selected_order_selectbox"
                )

                if selected_edit_order_id:
                    with get_db_connection() as conn:
                        order_header = pd.read_sql_query('SELECT * FROM Orders WHERE OrderID = ?', conn, params=(selected_edit_order_id,)).iloc[0]
                        sales_items_df = pd.read_sql_query('''
                            SELECT 
                                s.SaleID, 
                                s.ProductID, 
                                p.Name AS ProductName, 
                                s.BaseQuantitySold, 
                                s.SalePrice
                            FROM Sales s
                            LEFT JOIN Products p ON s.ProductID = p.ProductID
                            WHERE s.OrderID = ?
                        ''', conn, params=(selected_edit_order_id,))

                    all_cust_df = get_all_customers()
                    cust_names = all_cust_df['Name'].tolist()
                    current_cust_row = all_cust_df[all_cust_df['CustomerID'] == order_header['CustomerID']]
                    current_cust_name = current_cust_row.iloc[0]['Name'] if not current_cust_row.empty else (cust_names[0] if cust_names else "Market")

                    st.markdown(f"### ✏️ Editing Order `{selected_edit_order_id}`")

                    with st.form("edit_order_header_form"):
                        ec1, ec2, ec3 = st.columns(3)
                        if current_cust_name not in cust_names:
                            cust_names.append(current_cust_name)

                        updated_cust_name = ec1.selectbox("Customer Name", cust_names, index=cust_names.index(current_cust_name), key="edit_hdr_cust")
                        updated_date_placed = ec2.date_input("Date Placed", value=safe_parse_date(order_header['DatePlaced']), key="edit_hdr_dp")
                        updated_date_fulfilled = ec3.date_input("Date Fulfilled", value=safe_parse_date(order_header['DateFullfilled']), key="edit_hdr_df")

                        submit_header_btn = st.form_submit_button("💾 Save Header Details")

                        if submit_header_btn:
                            try:
                                c_id = get_or_create_customer(updated_cust_name)
                                get_or_create_order(selected_edit_order_id, c_id, str(updated_date_placed), str(updated_date_fulfilled))
                                st.success("Order header details updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error updating order header: {e}")

                    st.markdown("#### 🛒 Line Items in this Order")
                    db_prods = get_all_products()
                    all_db_pids = db_prods['ProductID'].tolist() if not db_prods.empty else []

                    edited_sales_df = st.data_editor(
                        sales_items_df,
                        num_rows="dynamic",
                        use_container_width=True,
                        key="edit_sales_editor",
                        column_config={
                            "SaleID": st.column_config.NumberColumn("Sale ID", disabled=True),
                            "ProductID": st.column_config.SelectboxColumn("Product ID", options=all_db_pids, required=True),
                            "ProductName": st.column_config.TextColumn("Product Name (DB Reference)", disabled=True),
                            "BaseQuantitySold": st.column_config.NumberColumn("Quantity Sold", step=0.1, required=True),
                            "SalePrice": st.column_config.NumberColumn("Sale Price ($)", step=1.0, required=True)
                        }
                    )

                    c_save, c_del = st.columns([3, 1])

                    with c_save:
                        if st.button("💾 Save Sales Line Item Changes", type="primary", key="btn_save_line_items"):
                            try:
                                with get_db_connection() as conn:
                                    cursor = conn.cursor()
                                    existing_sale_ids = set(sales_items_df['SaleID'].dropna().tolist())
                                    updated_sale_ids = set(edited_sales_df['SaleID'].dropna().tolist())

                                    deleted_ids = existing_sale_ids - updated_sale_ids
                                    for d_id in deleted_ids:
                                        cursor.execute("DELETE FROM Sales WHERE SaleID = ?", (d_id,))

                                    for _, row in edited_sales_df.iterrows():
                                        if pd.notna(row['SaleID']) and row['SaleID'] in existing_sale_ids:
                                            cursor.execute('''
                                                UPDATE Sales 
                                                SET ProductID = ?, BaseQuantitySold = ?, SalePrice = ?
                                                WHERE SaleID = ?
                                            ''', (row['ProductID'], float(row['BaseQuantitySold']), float(row['SalePrice']), row['SaleID']))
                                        else:
                                            cursor.execute('''
                                                INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice)
                                                VALUES (?, ?, ?, ?)
                                            ''', (selected_edit_order_id, row['ProductID'], float(row['BaseQuantitySold']), float(row['SalePrice'])))
                                    conn.commit()

                                st.success("Sales items updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error updating sales items: {e}")

                    with c_del:
                        if st.button("🗑️ Delete Entire Order", type="secondary", key="btn_delete_order"):
                            try:
                                with get_db_connection() as conn:
                                    cursor = conn.cursor()
                                    cursor.execute("DELETE FROM Sales WHERE OrderID = ?", (selected_edit_order_id,))
                                    cursor.execute("DELETE FROM Orders WHERE OrderID = ?", (selected_edit_order_id,))
                                    conn.commit()

                                st.success(f"Order `{selected_edit_order_id}` deleted successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error deleting order: {e}")