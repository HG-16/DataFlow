# Upload test data (fictional firms)

Upload in this order on the dashboard's **Upload Data** page:
1. `test_gst_registry.csv` as **GST registry** (13 rows: 9 imported, 4 blocked on purpose)
2. `test_vendors.csv` as **Vendors / bids** (14 rows: 13 imported incl. 1 duplicate merged, 1 blocked on purpose)
3. Click **Run checks on all vendors**

`expected_results.csv` lists what each row tests and the result you should see.
All firms, people, PANs and GSTINs are made up; valid GSTINs have correct check digits.
