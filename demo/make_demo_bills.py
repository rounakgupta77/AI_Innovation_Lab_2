"""Draws demo bills for the YouTube video into demo/bills/.

Dates are a few days before today, so run this shortly before recording.
The text layout is kept plain so the local OCR reads it reliably.
"""
import os
from datetime import date, timedelta

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bills")
WIDTH = 38  # characters per receipt line


def font(bold=False, size=30):
    names = ["consolab.ttf", "courbd.ttf"] if bold else ["consola.ttf", "cour.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(r"C:\Windows\Fonts", name), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


REGULAR, BOLD, BIG = font(), font(bold=True), font(bold=True, size=36)


def kv(label, value):
    return label + str(value).rjust(WIDTH - len(label))


def money(value):
    return f"{value:,.2f}"


def draw_paper(lines):
    """lines: list of (text, style) with style in center, big, bold, text, sep."""
    line_h, pad = 44, 50
    char_w = REGULAR.getbbox("M")[2]
    w = char_w * WIDTH + pad * 2
    h = line_h * len(lines) + pad * 2
    paper = Image.new("RGB", (w, h), (252, 251, 247))
    d = ImageDraw.Draw(paper)
    y = pad
    for text, style in lines:
        if style == "sep":
            d.line([(pad, y + line_h // 2), (w - pad, y + line_h // 2)], fill=(60, 60, 60), width=2)
        else:
            f = BIG if style == "big" else BOLD if style in ("bold", "center") else REGULAR
            tw = d.textlength(text, font=f)
            x = (w - tw) / 2 if style in ("center", "big") else pad
            d.text((x, y), text, font=f, fill=(25, 25, 25))
        y += line_h
    return paper


def as_photo(paper):
    """Puts the receipt on a table-like background with a soft shadow."""
    margin = 90
    bg = Image.new("RGB", (paper.width + margin * 2, paper.height + margin * 2), (196, 188, 174))
    shadow = Image.new("L", bg.size, 0)
    ImageDraw.Draw(shadow).rectangle(
        [margin + 12, margin + 16, margin + paper.width + 12, margin + paper.height + 16], fill=120)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    bg.paste((90, 80, 70), mask=shadow)
    bg.paste(paper, (margin, margin))
    return bg


def fuel_bill(station, tagline, address, gstin, bill_no, when, time, pump, rate, amount, paid_by):
    volume = amount / rate
    return [
        (station, "big"),
        (tagline, "center"),
        (address[0], "text"),
        (address[1], "text"),
        (f"GSTIN: {gstin}", "text"),
        ("", "sep"),
        (kv("Bill No:", bill_no), "text"),
        (kv(f"Date: {when:%d/%m/%Y}", f"Time: {time}"), "text"),
        (kv(f"Pump: {pump}", "Nozzle: 2"), "text"),
        (kv("Vehicle No:", "MH02 AB 4521"), "text"),
        ("", "sep"),
        (kv("Product:", "Petrol"), "text"),
        (kv("Rate (Rs/L):", money(rate)), "text"),
        (kv("Volume (L):", f"{volume:.2f}"), "text"),
        ("", "sep"),
        (kv("TOTAL AMOUNT:", "Rs. " + money(amount)), "bold"),
        ("", "sep"),
        (kv("Mode:", paid_by), "text"),
        ("Thank you! Visit again", "center"),
        ("*** Customer Copy ***", "center"),
    ]


def service_invoice(bill_no, when):
    items = [("Periodic Service Charge", 1200.00), ("Engine Oil 5W-30", 1450.00),
             ("Oil Filter", 290.00), ("Air Filter", 380.00), ("Wheel Alignment", 360.00)]
    subtotal = sum(v for _, v in items)
    gst = round(subtotal * 0.18, 2)
    return [
        ("AUTOCARE SERVICE CENTRE", "big"),
        ("Authorised Car Workshop", "center"),
        ("Shop 7, LBS Marg, Ghatkopar (W)", "text"),
        ("Mumbai 400086", "text"),
        ("GSTIN: 27AACFA5678K1Z2", "text"),
        ("", "sep"),
        (kv("Invoice No:", bill_no), "text"),
        (kv(f"Date: {when:%d/%m/%Y}", "Odometer: 41,250 km"), "text"),
        (kv("Vehicle No:", "MH02 AB 4521"), "text"),
        (kv("Model:", "Maruti Swift VXi"), "text"),
        ("", "sep"),
        (kv("Item", "Rs."), "bold"),
        *[(kv(name, money(value)), "text") for name, value in items],
        ("", "sep"),
        (kv("Sub Total", money(subtotal)), "text"),
        (kv("GST @ 18%", money(gst)), "text"),
        (kv("GRAND TOTAL:", "Rs. " + money(subtotal + gst)), "bold"),
        ("", "sep"),
        ("Thank you for choosing AutoCare", "center"),
    ]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    today = date.today()
    ago = lambda days: today - timedelta(days=days)  # noqa: E731

    bills = {
        "fuel_bill_1.jpg": fuel_bill(
            "SHREE SAI FUEL STATION", "Petrol & Diesel Retail Outlet",
            ("Plot 14, Link Road, Andheri (W)", "Mumbai 400053"), "27ABCPS1234F1Z5",
            "SSF/24817", ago(9), "09:42", "03", 105.00, 1980.00, "UPI"),
        "fuel_bill_2.jpg": fuel_bill(
            "HIGHWAY FILLING STATION", "Petrol & Diesel Retail Outlet",
            ("Western Express Hwy, Goregaon (E)", "Mumbai 400063"), "27AAHFH4410Q1Z9",
            "HFS-77302", ago(6), "18:15", "01", 105.00, 2050.00, "Card"),
        "fuel_bill_3.jpg": fuel_bill(
            "SHREE SAI FUEL STATION", "Petrol & Diesel Retail Outlet",
            ("Plot 14, Link Road, Andheri (W)", "Mumbai 400053"), "27ABCPS1234F1Z5",
            "SSF/25190", ago(3), "08:57", "05", 105.00, 1860.00, "UPI"),
        "fuel_bill_4.jpg": fuel_bill(  # the anomaly: 90 litres in one go, ~4.5x Amit's usual bill
            "GREENLINE PETROLEUM", "Petrol & Diesel Retail Outlet",
            ("Sion-Panvel Hwy, Vashi", "Navi Mumbai 400703"), "27AAGCG9021M1Z4",
            "GLP/10466", ago(1), "21:34", "02", 105.00, 9450.00, "Cash"),
    }
    for name, lines in bills.items():
        as_photo(draw_paper(lines)).save(os.path.join(OUT_DIR, name), "JPEG", quality=92)
        print("  created", name)

    pdf_name = "car_service_invoice.pdf"
    draw_paper(service_invoice("SRV-26-0817", ago(5))).save(
        os.path.join(OUT_DIR, pdf_name), "PDF", resolution=150)
    print("  created", pdf_name)
    print(f"\n  Bills saved in {OUT_DIR}")


if __name__ == "__main__":
    main()
