Attribute VB_Name = "EnviarMailing"
' Envía el mailing D-Industrimex desde Outlook (Windows), una fila por prospecto.
' Lee la hoja "Mailing" que genera:  python -m mailing --excel
'
' Columnas: A Correo · B Nombre · C Saludo · D Empresa · E Puesto · F Asunto · G CuerpoHTML · H Enviado
'
' Por seguridad arranca en MODO = "BORRADOR": deja cada correo en Borradores de Outlook
' para revisarlo. Cambia a "ENVIAR" solo cuando hayas revisado varios borradores.
' Las imágenes van incrustadas: la macro adjunta los archivos de la carpeta "imagenes"
' (junto a este libro) con su Content-ID, que es como las llama el HTML (cid:logo-rojo.png).
' Envía en lotes con pausa para no disparar los límites de Google Workspace / Microsoft 365.
' Las filas con fecha en "Enviado" se saltan, así que puedes correrla varias veces.

Option Explicit

Private Const MODO As String = "BORRADOR"     ' "BORRADOR" o "ENVIAR"
Private Const LOTE As Long = 40               ' correos por lote
Private Const PAUSA_SEG As Long = 60          ' pausa entre lotes
Private Const MAX_POR_CORRIDA As Long = 200   ' tope por corrida (Workspace permite ~2,000/día)
Private Const CUENTA As String = ""           ' ej. "pedro@dindustrimex.com"; vacío = cuenta por defecto
Private Const BAJAS As String = "ventas@dindustrimex.com"
Private Const PR_ATTACH_CONTENT_ID As String = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"

Public Sub EnviarMailing()
    Dim ws As Worksheet, ol As Object, cuentaOl As Object, mail As Object
    Dim fila As Long, ultima As Long, hechos As Long, correo As String

    Set ws = ThisWorkbook.Worksheets("Mailing")
    ultima = ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
    Set ol = CreateObject("Outlook.Application")
    If CUENTA <> "" Then Set cuentaOl = BuscarCuenta(ol, CUENTA)

    If MODO = "ENVIAR" Then
        If MsgBox("Se van a ENVIAR hasta " & MAX_POR_CORRIDA & " correos. ¿Continuar?", _
                  vbYesNo + vbExclamation, "Mailing D-Industrimex") <> vbYes Then Exit Sub
    End If

    For fila = 2 To ultima
        correo = Trim(ws.Cells(fila, "A").Value)
        If correo <> "" And ws.Cells(fila, "H").Value = "" Then
            Set mail = ol.CreateItem(0)
            With mail
                .To = correo
                .Subject = ws.Cells(fila, "F").Value
                .HTMLBody = ws.Cells(fila, "G").Value
                AdjuntarImagenes mail, CStr(ws.Cells(fila, "G").Value)
                If Not cuentaOl Is Nothing Then Set .SendUsingAccount = cuentaOl
                On Error Resume Next
                .PropertyAccessor.SetProperty _
                    "http://schemas.microsoft.com/mapi/string/{00020386-0000-0000-C000-000000000046}/List-Unsubscribe", _
                    "<mailto:" & BAJAS & "?subject=Baja%20de%20correos&body=" & correo & ">"
                On Error GoTo 0
                If MODO = "ENVIAR" Then .Send Else .Save
            End With
            ws.Cells(fila, "H").Value = IIf(MODO = "ENVIAR", "Enviado ", "Borrador ") & Format(Now, "yyyy-mm-dd hh:nn")
            hechos = hechos + 1
            If hechos >= MAX_POR_CORRIDA Then Exit For
            If hechos Mod LOTE = 0 Then Application.Wait Now + TimeSerial(0, 0, PAUSA_SEG)
        End If
    Next fila

    ThisWorkbook.Save
    MsgBox hechos & IIf(MODO = "ENVIAR", " correos enviados.", " borradores en Outlook."), vbInformation
End Sub

Private Sub AdjuntarImagenes(mail As Object, html As String)
    Dim carpeta As String, archivo As String, adj As Object
    carpeta = ThisWorkbook.Path & Application.PathSeparator & "imagenes" & Application.PathSeparator
    archivo = Dir(carpeta & "*.*")
    Do While archivo <> ""
        If InStr(1, html, "cid:" & archivo, vbTextCompare) > 0 Then
            Set adj = mail.Attachments.Add(carpeta & archivo, 1, 0)
            adj.PropertyAccessor.SetProperty PR_ATTACH_CONTENT_ID, archivo
        End If
        archivo = Dir()
    Loop
End Sub

Private Function BuscarCuenta(ol As Object, smtp As String) As Object
    Dim c As Object
    For Each c In ol.Session.Accounts
        If LCase(c.SmtpAddress) = LCase(smtp) Then Set BuscarCuenta = c: Exit Function
    Next c
    Err.Raise vbObjectError + 1, , "No encontré la cuenta " & smtp & " en Outlook"
End Function
