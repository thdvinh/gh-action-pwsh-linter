$obj = @{ Name = "Test"; Age = 30 }
if ($obj -eq "Test") {
    Write-Output "Compare object with string"
} else {
    Write-Output "Object does not equal string"
}
Write-Output $obj
Write-Output "Name $($obj.Name)"
Write-Output "Age $($obj.Age)"