"""Example usage of SalesIntellectClient.

Requires SI_API_TOKEN to be set in the environment:
    export SI_API_TOKEN=your_token_here
    python example.py
"""

from client import SalesIntellectClient

client = SalesIntellectClient()

products = client.list_products()
print("Products:", products)

# Adjust the below to a real shop_id / product_id from your account.
shop_id = "SHOP_ID_HERE"
product_id = "PRODUCT_ID_HERE"

inventory = client.get_inventory(shop_id)
print("Current inventory:", inventory)

result = client.adjust_inventory(shop_id, product_id, delta=5)
print("After +5 adjustment:", result)
