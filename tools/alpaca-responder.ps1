# Alpaca discovery responder (no-Python fallback for a Windows NINA PC).
#
# Use this together with a built-in netsh TCP forward when you don't want to install
# Python. It answers NINA's Alpaca discovery broadcast so NINA finds the forwarded device.
#
# One-time TCP forward (run once in an ELEVATED cmd/PowerShell; persists across reboots):
#   netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=11111 connectaddress=<PI_IP> connectport=11111
#   netsh advfirewall firewall add rule name="Alpaca 11111" dir=in action=allow protocol=TCP localport=11111
#   netsh advfirewall firewall add rule name="Alpaca discovery" dir=in action=allow protocol=UDP localport=32227
# (netsh portproxy needs the "IP Helper" service (iphlpsvc) running, even for v4tov4.)
#
# Then run this responder (leave the window open, or add to Task Scheduler at startup):
#   powershell -ExecutionPolicy Bypass -File alpaca-responder.ps1
#
# $AlpacaPort MUST match the netsh listenport above.

$DiscoveryPort = 32227
$AlpacaPort    = 11111

$reply = [System.Text.Encoding]::ASCII.GetBytes('{"AlpacaPort": ' + $AlpacaPort + '}')
$udp = [System.Net.Sockets.UdpClient]::new(
    [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, $DiscoveryPort))
Write-Host "Alpaca discovery responder on udp/$DiscoveryPort -> AlpacaPort $AlpacaPort (Ctrl+C to stop)"
while ($true) {
    $remote = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)
    $data = $udp.Receive([ref]$remote)
    if ([System.Text.Encoding]::ASCII.GetString($data) -like '*alpacadiscovery1*') {
        $udp.Send($reply, $reply.Length, $remote) | Out-Null
        Write-Host "replied to $($remote.Address)"
    }
}
