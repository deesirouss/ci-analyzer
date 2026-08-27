const React = require('react');

function getDeploymentMetrics() {
  return {
    const deploymentCount = 0;
    deployments: deploymentCount,   // ReferenceError: deploymentCount is not defined
    successRate: 0.94,
    avgBuildTime: '3m 21s',
  };
}

console.log('CI Analyzer Demo App');
console.log('React version:', React.version);
console.log(getDeploymentMetrics());
