# Network Monitor (Python + Flask)

**Ελαφρύ, σύγχρονο εργαλείο για real-time παρακολούθηση της διαθεσιμότητας και του latency σε βασικά hosts του δικτύου σας.**
Εμφανίζει status, ιστορικό, απώλειες και στατιστικά μέσω responsive web UI. Κατάλληλο για μικρά γραφεία, server rooms, εργαστήρια, ISPs ή power users.

---

## Χαρακτηριστικά

- **Εύκολη παραμετροποίηση** – Δηλώνεις τα hosts/IPs σε ένα array.
- **Έξυπνο fallback** – ICMP (ping) με TCP fallback αν είναι μπλοκαρισμένο.
- **Logging σε CSV** – Ιστορικό σε αρχείο δίπλα στο script.
- **Web interface (Flask)** – Πλήρως responsive, με αυτόματο refresh, KPIs, alarm banner & ιστορικά διαγράμματα ανά host.
- **Ομαδική επίβλεψη** – Φίλτρα, αναζήτηση, sorting, real-time ειδοποιήσεις.

---

## Απαιτήσεις

- Python 3.8+
- Flask (`pip install flask`)

---

## Οδηγίες Εκτέλεσης

1. **Κατέβασε το script ή το repo**
2. Εγκατέστησε το Flask:
   ```bash
   pip install flask
   ```
3. Εκτέλεσε το πρόγραμμα με:
   ```bash
   python monitor.py
   ```
4. Άνοιξε τον browser στη διεύθυνση:
   ```
   http://localhost:5000
   ```
   Ή, αν τρέχεις το script σε server, από άλλο PC στο ίδιο δίκτυο (π.χ. `http://192.168.1.20:5000`).

---

## Παραμετροποίηση

Επεξεργάζεσαι το array `HOSTS` στην αρχή του αρχείου:
```python
HOSTS = [
    {"name": "Router", "host": "192.168.1.1"},
    {"name": "Google DNS", "host": "8.8.8.8"},
    # Προσθέτεις όσα hosts/IPs θες
]
