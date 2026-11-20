type CartItem = { sku: string; quantity: number };
type CheckoutRequest = { email: string; items: CartItem[] };

export async function checkout(request: CheckoutRequest) {
  const response = await fetch("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  if (!response.ok) throw new Error("Checkout failed");
  return response.json() as Promise<{ orderId: string }>;
}
