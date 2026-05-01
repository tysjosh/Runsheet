import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface ReportViewerProps {
  content: string;
  onClose: () => void;
}

export default function ReportViewer({ content, onClose }: ReportViewerProps) {
  const handleDownloadPDF = () => {
    const printWindow = window.open("", "_blank");
    if (printWindow) {
      // Convert markdown tables to proper HTML
      const convertMarkdownTables = (md: string): string => {
        const lines = md.split("\n");
        const result: string[] = [];
        let inTable = false;
        let headerDone = false;

        for (let i = 0; i < lines.length; i++) {
          const line = lines[i].trim();
          const isTableRow = /^\|(.+)\|$/.test(line);
          const isSeparator =
            /^\|[\s\-:|]+\|$/.test(line) &&
            line.replace(/[|\s\-:]/g, "").length === 0;

          if (isTableRow) {
            if (!inTable) {
              result.push("<table>");
              inTable = true;
              headerDone = false;
            }
            if (isSeparator) {
              headerDone = true;
              continue;
            }
            const rawCells = line.split("|");
            const cleanCells = rawCells
              .slice(1, rawCells.length - 1)
              .map((c) => c.trim());

            if (!headerDone) {
              result.push(
                "<tr>" +
                  cleanCells.map((c) => `<th>${c}</th>`).join("") +
                  "</tr>"
              );
            } else {
              result.push(
                "<tr>" +
                  cleanCells.map((c) => `<td>${c}</td>`).join("") +
                  "</tr>"
              );
            }
          } else {
            if (inTable) {
              result.push("</table>");
              inTable = false;
              headerDone = false;
            }
            result.push(line);
          }
        }
        if (inTable) result.push("</table>");
        return result.join("\n");
      };

      const htmlContent = convertMarkdownTables(content)
        .replace(/^### (.*$)/gm, "<h3>$1</h3>")
        .replace(/^## (.*$)/gm, "<h2>$1</h2>")
        .replace(/^# (.*$)/gm, "<h1>$1</h1>")
        .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.*?)\*/g, "<em>$1</em>")
        .replace(/^- (.*$)/gm, "<li>$1</li>")
        .replace(/\n\n/g, "<br><br>")
        .replace(/\n/g, "<br>");

      printWindow.document.write(`
        <!DOCTYPE html>
        <html>
        <head>
          <title>Logistics Report</title>
          <style>
            body {
              font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
              line-height: 1.6;
              color: #333;
              max-width: 800px;
              margin: 0 auto;
              padding: 20px;
            }
            h1 { color: #232323; border-bottom: 2px solid #232323; padding-bottom: 10px; }
            h2 { color: #232323; margin-top: 30px; }
            h3 { color: #4b5563; }
            ul { padding-left: 20px; list-style-type: disc; }
            li { margin: 5px 0; }
            strong { color: #232323; }
            em { color: #6b7280; }
            table { width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; }
            th { background: #f3f4f6; text-align: left; padding: 8px 12px; border: 1px solid #d1d5db; font-weight: 600; color: #374151; }
            td { padding: 6px 12px; border: 1px solid #e5e7eb; color: #4b5563; }
            tr:nth-child(even) { background: #f9fafb; }
            @media print {
              body { margin: 0; padding: 15px; }
              .no-print { display: none; }
            }
          </style>
        </head>
        <body>
          ${htmlContent}
        </body>
        </html>
      `);
      printWindow.document.close();
      printWindow.print();
    }
  };

  const handleCopyToClipboard = () => {
    navigator.clipboard.writeText(content);
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50 p-4">
      <div className="bg-white rounded-lg shadow-xl max-w-4xl w-full max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between p-4 border-b border-gray-200">
          <h2 className="text-xl font-bold text-[#232323]">Report Viewer</h2>
          <div className="flex gap-2">
            <button
              onClick={handleCopyToClipboard}
              className="px-3 py-2 text-sm bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors"
            >
              Copy
            </button>
            <button
              onClick={handleDownloadPDF}
              className="px-3 py-2 text-sm bg-[#232323] hover:bg-[#333333] text-white rounded-lg transition-colors"
            >
              Print/PDF
            </button>
            <button
              onClick={onClose}
              className="px-3 py-2 text-sm bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg transition-colors"
            >
              Close
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                h1: ({ children }) => (
                  <h1 className="text-2xl font-bold text-[#232323] border-b-2 border-[#232323] pb-2 mb-4">
                    {children}
                  </h1>
                ),
                h2: ({ children }) => (
                  <h2 className="text-xl font-semibold text-gray-800 mt-6 mb-3">
                    {children}
                  </h2>
                ),
                h3: ({ children }) => (
                  <h3 className="text-lg font-medium text-gray-700 mt-4 mb-2">
                    {children}
                  </h3>
                ),
                ul: ({ children }) => (
                  <ul className="list-disc list-inside space-y-1 mb-4">
                    {children}
                  </ul>
                ),
                li: ({ children }) => (
                  <li className="text-gray-700">{children}</li>
                ),
                p: ({ children }) => (
                  <p className="text-gray-700 mb-3">{children}</p>
                ),
                strong: ({ children }) => (
                  <strong className="font-semibold text-[#232323]">
                    {children}
                  </strong>
                ),
                em: ({ children }) => (
                  <em className="italic text-gray-600">{children}</em>
                ),
                table: ({ children }) => (
                  <div className="overflow-x-auto my-3">
                    <table className="w-full text-sm border-collapse border border-gray-200 rounded">
                      {children}
                    </table>
                  </div>
                ),
                th: ({ children }) => (
                  <th className="bg-gray-100 text-left px-3 py-2 border border-gray-200 font-semibold text-gray-700 text-xs">
                    {children}
                  </th>
                ),
                td: ({ children }) => (
                  <td className="px-3 py-1.5 border border-gray-100 text-gray-600 text-xs">
                    {children}
                  </td>
                ),
              }}
            >
              {content}
            </ReactMarkdown>
          </div>
        </div>
      </div>
    </div>
  );
}
