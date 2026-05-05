import pythoncom
import win32com.client

pythoncom.CoInitialize()
app = win32com.client.GetActiveObject("Word.Application")
doc = app.ActiveDocument
print(f"len(doc.Content.Text) = {len(doc.Content.Text)}")
print(f"doc.Content.End       = {doc.Content.End}")
print(f"doc.Content.Start     = {doc.Content.Start}")
