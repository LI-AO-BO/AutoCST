function response = autocst(command, value, pythonExe)
%AUTOCST Call the same local runner used by the assistant.
%   autocst("doctor")
%   r = autocst("submit", fullfile(projectRoot,"examples","wr90.json"));
%   autocst("status", r.run_id)
%   autocst("results", r.run_id)
%   autocst("research", ["context", experimentId])
%   autocst("research", ["prepare", experimentId, jobFile, "--decision", decisionFile])
%   autocst("research", ["submit-prepared", preparedId, "--idempotency-key", "step-1"])
%   autocst("research", ["events", "--experiment-id", experimentId, "--after", "0"])
% A subprocess avoids MATLAB/Python in-process ABI coupling.
arguments
    command (1,1) string {mustBeMember(command,["doctor","submit","status","results","research"])}
    value (1,:) string = ""
    pythonExe (1,1) string = ""
end
root = string(fileparts(fileparts(mfilename('fullpath'))));
if pythonExe == ""
    pythonExe = fullfile(root, ".venv", "Scripts", "python.exe");
end
if ~isfile(pythonExe)
    error("AutoCST:PythonMissing", "Run setup.ps1 first, or provide pythonExe.");
end
% ProcessBuilder passes each argument directly; paths never become shell code.
items = {char(pythonExe), '-m', 'autocst', '--root', char(root), char(command)};
if command == "research"
    if isempty(value) || any(strlength(value) == 0)
        error("AutoCST:MissingArgument", "Provide a research subcommand and its arguments as a string array.");
    end
    % Every token remains a distinct ProcessBuilder argument, including spaces.
    for k = 1:numel(value)
        items{end+1} = char(value(k));
    end
elseif command ~= "doctor"
    if numel(value) ~= 1 || value == ""
        error("AutoCST:MissingArgument", "This command requires a file or run ID.");
    end
    items{end+1} = char(value);
end
args = java.util.ArrayList();
for k = 1:numel(items)
    args.add(java.lang.String(items{k}));
end
builder = java.lang.ProcessBuilder(args);
builder.directory(java.io.File(char(root)));
builder.environment().put('PYTHONIOENCODING','utf-8');
builder.redirectErrorStream(true);
process = builder.start();
scanner = java.util.Scanner(process.getInputStream(), 'UTF-8');
scanner.useDelimiter('\A');
text = "";
if scanner.hasNext()
    text = string(scanner.next());
end
code = process.waitFor();
scanner.close();
if code ~= 0
    error("AutoCST:RunnerFailed", "%s", text);
end
response = jsondecode(text);
end
