"""
Connect test then place one order on the SAME connection.
Proves socket works (currentTime), then sends placeOrder. If 320 happens, we see it.
No reqOpenOrders. Run: python ibkr_connect_then_order.py

If you get timeout and no order: TWS 10.43 + old ibapi (9.x) can cause Error 320.
Upgrade: pip install pfund-ibapi   (or install official TWS API 10.x from IBKR).
"""
import sys
import threading
import time

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    from ibapi.order import Order
except ImportError:
    print("Need ibapi: pip install ibapi  (or pip install pfund-ibapi for 10.x)")
    sys.exit(1)

# Warn if ibapi is 9.x and TWS is likely 10.x (version mismatch → 320)
try:
    import ibapi
    ver = getattr(ibapi, "__version__", "") or "?"
    if ver.startswith("9."):
        print("  [Note] ibapi version", ver, "- TWS 10.x may need API 10.x (pip install pfund-ibapi)\n")
except Exception:
    pass

HOST = "127.0.0.1"
PORT = 7497
CLIENT_ID = 1
SYMBOL = "TSLA"
QUANTITY = 1
CONNECT_TIMEOUT = 10
ORDER_WAIT = 15


class App(EWrapper, EClient):
    def __init__(self):
        self.connected = threading.Event()
        self.next_valid_id = None
        self.time_received = threading.Event()
        self.order_done = threading.Event()
        self.current_order_id = None
        self.last_order_status = None
        self.errors = []
        EClient.__init__(self, self)

    def nextValidId(self, orderId: int):
        self.next_valid_id = orderId
        self.connected.set()
        print(f"  [OK] nextValidId({orderId})")

    def currentTime(self, time_from_server: int):
        self.time_received.set()
        print(f"  [OK] currentTime({time_from_server}) - round-trip OK.")

    def openOrder(self, orderId, contract, order, orderState):
        if orderId != getattr(self, "current_order_id", -999):
            return
        self.last_order_status = getattr(orderState, "status", None) or "OpenOrder"
        print(f"  [OK] openOrder {orderId}: {self.last_order_status}")
        self.order_done.set()

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        self.last_order_status = status
        print(f"  [OK] orderStatus {orderId}: {status}")
        if status in ("Submitted", "Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.order_done.set()

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        """API 10.x adds errorTime as second arg."""
        self.errors.append((errorCode, errorString))
        if errorCode == 320:
            self.order_done.set()
            print(f"  [!!] Error 320: Socket I/O error - connection broke.")
            return
        if reqId == getattr(self, "current_order_id", -999):
            self.order_done.set()
            print(f"  [--] Error (order {reqId}): {errorCode} - {errorString}")
            return
        if errorCode in (2104, 2106, 2158):
            return
        print(f"  [--] Error: {errorCode} - {errorString}")


def main():
    print("IBKR: connect -> currentTime -> place order (same connection)\n")
    print(f"  {HOST}:{PORT}  ClientId:{CLIENT_ID}  BUY {QUANTITY} {SYMBOL} MKT\n")

    app = App()
    app.connect(HOST, PORT, clientId=CLIENT_ID)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()

    if not app.connected.wait(timeout=CONNECT_TIMEOUT):
        print("  [FAIL] Could not connect.")
        sys.exit(1)
    print("  [OK] Connected.")
    time.sleep(1)  # let server version handshake complete (API 10.x)

    app.reqCurrentTime()
    if not app.time_received.wait(timeout=5):
        print("  [--] No currentTime (continuing anyway).")
    print("")

    # Place order on same connection (no reqOpenOrders)
    contract = Contract()
    contract.symbol = SYMBOL
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    order = Order()
    order.action = "BUY"
    order.orderType = "MKT"
    order.totalQuantity = QUANTITY
    order.tif = "DAY"
    order.eTradeOnly = False
    order.firmQuoteOnly = False

    order_id = app.next_valid_id
    app.order_done.clear()
    app.current_order_id = order_id
    app.placeOrder(order_id, contract, order)
    print(f"  Placed: BUY {QUANTITY} {SYMBOL} MKT (id={order_id})\n")

    if app.order_done.wait(timeout=ORDER_WAIT):
        print(f"  Result: {app.last_order_status or 'OK'}\n")
    else:
        print("  No response (timeout).\n")

    if any(c == 320 for c, _ in app.errors):
        print("  [FAIL] Error 320 - try IB Gateway instead of TWS, or check firewall.")

    time.sleep(1)
    app.disconnect()
    print("  Done.")


if __name__ == "__main__":
    main()
